import os
import json
import logging
from decimal import Decimal

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ── Env ───────────────────────────────────────────────────────────────────────
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
HL_ADDRESS = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
HL_PRIVKEY = os.getenv("HYPERLIQUID_SECRET_KEY", "").strip()  # 0x… private key
DEFAULT_NOTIONAL = Decimal(os.getenv("DEFAULT_NOTIONAL_USD", "50"))
DEFAULT_SLIPPAGE = float(os.getenv("DEFAULT_SLIPPAGE", "0.02"))  # 2% default

ALLOWED_SYMBOLS = set(
    s.strip().upper() for s in os.getenv(
        "ALLOWED_SYMBOLS", "BTC,ETH,SOL,LINK,BNB,AVAX"
    ).split(",") if s.strip()
)

# ── Exchange init (CCXT) ──────────────────────────────────────────────────────
def make_exchange():
    hostname = "hyperliquid-testnet.xyz" if USE_TESTNET else "hyperliquid.xyz"
    api_wallet = os.getenv("HYPERLIQUID_API_WALLET_ADDRESS", "").strip()

    ex = ccxt.hyperliquid({
        # HL via CCXT:
        "walletAddress": HL_ADDRESS,     # owner/user wallet (the one with the balance)
        "privateKey": HL_PRIVKEY,        # API wallet PRIVATE KEY
        "options": {
            "defaultType": "swap",
            "defaultSlippage": DEFAULT_SLIPPAGE,
            # provide API wallet address explicitly (names used by CCXT/HL adapter)
            "apiWalletAddress": api_wallet,
            "vaultAddress": api_wallet,  # some versions use 'vaultAddress' internally
        },
        "urls": {
            "api": {
                "public": f"https://api.{hostname}",
                "private": f"https://api.{hostname}",
            }
        }
    })
    ex.load_markets()
    return ex


try:
    ex = make_exchange()
    log.info(f"✅ Connected to Hyperliquid via CCXT ({'testnet' if USE_TESTNET else 'mainnet'})")
except Exception as e:
    log.exception("Failed to initialize exchange")
    ex = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def to_hl_symbol(sym: str) -> str:
    s = (sym or "BTC").upper()
    s = s.replace("USDT", "USDC").replace("USD", "USDC").replace("/USDC", "")
    base = s.split("/")[0]
    for m in ex.markets.values():
        if m.get("type") == "swap" and m.get("base") == base:
            return m["symbol"]
    raise ValueError(f"No HL swap market found for base '{base}'")

def amount_to_precision(symbol: str, amount) -> str:
    # Always let CCXT format amounts
    return ex.amount_to_precision(symbol, float(amount))

def compute_market_size(symbol: str, notional: Decimal) -> str:
    t = ex.fetch_ticker(symbol)
    px = t.get("last") or t.get("close") or t.get("info", {}).get("markPx")
    if not px:
        raise ValueError("Price unavailable for symbol")
    px = Decimal(str(px))
    if px <= 0:
        raise ValueError("Invalid price from ticker")
    raw = notional / px
    return amount_to_precision(symbol, raw)

def parse_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "t", "yes", "y")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    mode = "active" if (ex and HL_ADDRESS and HL_PRIVKEY) else "demo"
    try:
        bal = ex.fetch_balance() if ex else {}
        withdrawable = (bal.get("USDC", {}) or {}).get("free") \
                       or (bal.get("total", {}) or {}).get("USDC")
    except Exception:
        withdrawable = None
    return jsonify({
        "status": "healthy",
        "trading": mode,
        "network": "testnet" if USE_TESTNET else "mainnet",
        "credentials_set": bool(HL_ADDRESS and HL_PRIVKEY),
        "balance": withdrawable
    })

@app.get("/")
def home():
    return jsonify({
        "message": "TradingView → Hyperliquid (CCXT) webhook",
        "endpoints": {"health": "GET /health", "webhook": "POST /webhook/tradingview"},
        "network": "testnet" if USE_TESTNET else "mainnet"
    })

@app.post("/webhook/tradingview")
def tradingview():
    if not ex or not (HL_ADDRESS and HL_PRIVKEY):
        return jsonify({"status": "demo", "message": "[DEMO] Credentials missing or exchange not ready"}), 200

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"status": "error", "message": "Invalid or missing JSON"}), 400

    log.info(f"Received alert: {payload}")

    symbol_in = (payload.get("symbol") or payload.get("coin") or "BTC")
    action = (payload.get("action") or payload.get("side") or "buy").lower()
    reduce_only = parse_bool(payload.get("reduce_only") or payload.get("reduceOnly"), False)
    post_only = parse_bool(payload.get("post_only") or payload.get("postOnly"), False)
    tif = (payload.get("tif") or payload.get("time_in_force") or "IOC").upper()  # IOC/GTC/FOK
    notional = payload.get("notional")
    qty = payload.get("quantity") or payload.get("qty")

    base = symbol_in.split(":")[0].split("/")[0].upper()
    if ALLOWED_SYMBOLS and base not in ALLOWED_SYMBOLS:
        return jsonify({"status": "blocked", "message": f"Symbol '{base}' not in ALLOWED_SYMBOLS"}), 403

    try:
        hl_symbol = to_hl_symbol(symbol_in)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Symbol resolve failed: {e}"}), 400

    side = "buy" if action in ("buy", "long") else "sell"

    # Determine amount
    try:
        if qty is not None:
            amount = amount_to_precision(hl_symbol, Decimal(str(qty)))
        else:
            notional_usd = Decimal(str(notional)) if notional is not None else DEFAULT_NOTIONAL
            amount = compute_market_size(hl_symbol, notional_usd)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Size calc failed: {e}"}), 400

    params = {"reduceOnly": reduce_only}
    if tif in ("IOC", "FOK", "GTC"):
        params["timeInForce"] = tif

    try:
        if post_only:
            # Post-only must be limit
            params["postOnly"] = True
            t = ex.fetch_ticker(hl_symbol)
            last = Decimal(str(t.get("last") or t.get("close")))
            # Slight nudge to keep order on book
            limit_px = float(last * (Decimal("0.999") if side == "sell" else Decimal("1.001")))
            order = ex.create_order(hl_symbol, "limit", side, float(amount), limit_px, params)
        else:
            # MARKET: HL CCXT requires a reference price and (optionally) a slippage
            t = ex.fetch_ticker(hl_symbol)
            last = float(t.get("last") or t.get("close"))
            order = ex.create_order(
                hl_symbol,
                "market",
                side,
                float(amount),
                last,                                 # reference price required
                {**params, "slippage": DEFAULT_SLIPPAGE}  # e.g., 0.02 = 2%
            )

        return jsonify({
            "status": "success",
            "message": f"Executed {side.upper()} {amount} {hl_symbol} ({'limit postOnly' if post_only else 'market'})",
            "result": order
        })
    except ccxt.BaseError as e:
        log.exception("Exchange error")
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        log.exception("Unhandled error")
        return jsonify({"status": "error", "message": str(e)}), 500

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
