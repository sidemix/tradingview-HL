import os
import math
import json
import logging
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import ccxt
from eth_account import Account

# ----------------------------
# Boot + logging
# ----------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("webhook")

# ----------------------------
# ENV & constants
# ----------------------------
USE_TESTNET = os.getenv("USE_TESTNET", "true").strip().lower() == "true"

OWNER_ADDR = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
API_ADDR_ENV = os.getenv("HYPERLIQUID_API_WALLET_ADDRESS", "").strip()
PRIV = os.getenv("HYPERLIQUID_SECRET_KEY", "").strip()

DEFAULT_NOTIONAL_USD = float(os.getenv("DEFAULT_NOTIONAL_USD", "50"))
DEFAULT_SLIPPAGE = float(os.getenv("DEFAULT_SLIPPAGE", "0.02"))  # 2%
ALLOWED_SYMBOLS = [s.strip().upper() for s in os.getenv(
    "ALLOWED_SYMBOLS", "BTC,ETH,SOL,LINK,BNB,AVAX"
).split(",") if s.strip()]

# ----------------------------
# Helpers
# ----------------------------
def _normalize_hex(x: str) -> str:
    x = (x or "").strip()
    return x if x.startswith("0x") else ("0x" + x)

def _safe_float(x, default=None):
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, Decimal):
            return float(x)
        if isinstance(x, str):
            return float(x.strip())
        return default
    except (ValueError, InvalidOperation, TypeError):
        return default

def to_hl_symbol(sym: str) -> str:
    """CCXT HL symbols are like 'BTC/USD' (perp)."""
    s = (sym or "").upper().replace("USDT", "").replace("/USD", "").strip()
    return f"{s}/USD"

def build_urls():
    host = "hyperliquid-testnet.xyz" if USE_TESTNET else "hyperliquid.xyz"
    base = f"https://api.{host}"
    return {
        "api": {
            "public": base,
            "private": base,
        }
    }

def derive_addr_from_priv(priv_hex: str) -> str:
    return Account.from_key(_normalize_hex(priv_hex)).address  # checksum 0x...

# ----------------------------
# Exchange init with HARD CHECK
# ----------------------------
if not OWNER_ADDR:
    raise RuntimeError("HYPERLIQUID_ACCOUNT_ADDRESS is not set")
if not API_ADDR_ENV:
    raise RuntimeError("HYPERLIQUID_API_WALLET_ADDRESS is not set")
if not PRIV:
    raise RuntimeError("HYPERLIQUID_SECRET_KEY is not set")

DERIVED_ADDR = derive_addr_from_priv(PRIV)

log.info(f"HL base_url: https://api.{'hyperliquid-testnet.xyz' if USE_TESTNET else 'hyperliquid.xyz'}")
log.info(f"OWNER (balance wallet): {OWNER_ADDR}")
log.info(f"API_ADDR_ENV:           {API_ADDR_ENV}")
log.info(f"API_ADDR_DERIVED:       {DERIVED_ADDR}")
log.info(f"Network:                {'testnet' if USE_TESTNET else 'mainnet'}")

if DERIVED_ADDR.lower() != API_ADDR_ENV.lower():
    raise RuntimeError(
        "API wallet mismatch:\n"
        f"  Derived from PRIVATE KEY: {DERIVED_ADDR}\n"
        f"  Env API wallet address :  {API_ADDR_ENV}\n"
        "Fix: paste the PRIVATE KEY that belongs to the API wallet above, "
        "or update HYPERLIQUID_API_WALLET_ADDRESS to the derived address and create/authorize it on the correct network."
    )

def make_exchange():
    ex = ccxt.hyperliquid({
        "walletAddress": OWNER_ADDR,                 # owner wallet (has balance)
        "privateKey": _normalize_hex(PRIV),          # API wallet PRIVATE KEY
        "options": {
            "defaultType": "swap",
            "defaultSlippage": DEFAULT_SLIPPAGE,     # CCXT helper slippage (some builds use this)
            "apiWalletAddress": API_ADDR_ENV,        # pass explicit API/vault address
            "vaultAddress": API_ADDR_ENV,
        },
        "urls": build_urls(),
    })
    ex.load_markets()
    log.info("✅ Connected to Hyperliquid via CCXT (%s)", "testnet" if USE_TESTNET else "mainnet")
    return ex

ex = make_exchange()

# ----------------------------
# Flask app
# ----------------------------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({
        "message": "TradingView → Hyperliquid (CCXT) webhook",
        "endpoints": {
            "health": "/health",
            "whoami": "/whoami",
            "webhook": "/webhook/tradingview"
        },
        "network": "testnet" if USE_TESTNET else "mainnet",
        "allowed_symbols": ALLOWED_SYMBOLS
    })

@app.get("/health")
def health():
    bal = None
    try:
        # fetch balance via HL info endpoint exposed by CCXT
        # some HL builds expose 'balance' in fetchBalance()['info']
        b = ex.fetch_balance()
        # Try common places
        bal = b.get("total", {}).get("USD") or b.get("info", {}).get("withdrawable")
    except Exception:
        pass

    return jsonify({
        "status": "healthy",
        "trading": "active",
        "balance": bal,
        "credentials_set": True,
        "network": "testnet" if USE_TESTNET else "mainnet"
    })

@app.get("/whoami")
def whoami():
    return jsonify({
        "ownerWallet": OWNER_ADDR,
        "apiWallet_env": API_ADDR_ENV,
        "apiWallet_from_privateKey": DERIVED_ADDR,
        "network": "testnet" if USE_TESTNET else "mainnet"
    })

def compute_amount_from_notional(symbol: str, notional_usd: float) -> float:
    """Convert USD notional to coin size using last price."""
    ticker = ex.fetch_ticker(symbol)
    last = _safe_float(ticker.get("last"))
    if not last or last <= 0:
        raise RuntimeError(f"Could not fetch last price for {symbol}")
    return float(notional_usd) / float(last)

def place_order(hl_symbol: str, side: str, amount: float, price: float | None, params: dict):
    """
    Create either a limit or market order.
    For market orders, CCXT HL requires a max-slippage inferred price.
    We rely on DEFAULT_SLIPPAGE and CCXT adapter; to be safe, pass 'slippage' in params.
    """
    side = side.lower()
    if price is not None:
        # Limit
        return ex.create_order(hl_symbol, "limit", side, float(amount), float(price), params)
    else:
        # Market
        return ex.create_order(hl_symbol, "market", side, float(amount), None, {**params, "slippage": DEFAULT_SLIPPAGE})

@app.post("/webhook/tradingview")
def tradingview():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400

    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    log.info("Received alert: %s", data)

    # ---- Parse basic fields ----
    symbol_in = (data.get("symbol") or "").upper().strip()
    action = (data.get("action") or "").lower().strip()  # "buy"/"sell", "long"/"short"
    quantity = _safe_float(data.get("quantity"))
    notional = _safe_float(data.get("notional"))
    tif = (data.get("tif") or "GTC").upper().strip()     # IOC / GTC
    post_only = bool(data.get("post_only", False))
    reduce_only = bool(data.get("reduce_only", False))
    limit_px = _safe_float(data.get("price"))            # optional limit price

    if not symbol_in:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    base = symbol_in.replace("/USD", "").replace("USDT", "").strip()
    if ALLOWED_SYMBOLS and base not in ALLOWED_SYMBOLS:
        return jsonify({"status": "error", "message": f"symbol {base} not allowed"}), 400

    side = "buy"
    if action in ("sell", "short"):
        side = "sell"
    elif action in ("buy", "long"):
        side = "buy"
    else:
        return jsonify({"status": "error", "message": "action must be buy/sell (or long/short)"}), 400

    hl_symbol = to_hl_symbol(base)

    # ---- Build CCXT params ----
    params = {}
    # time in force
    if tif in ("IOC", "GTC"):
        params["timeInForce"] = tif
    # flags
    if post_only:
        params["postOnly"] = True
    if reduce_only:
        params["reduceOnly"] = True

    # ---- Determine amount (size) ----
    amount = None
    if quantity is not None:
        amount = float(quantity)
    elif notional is not None:
        amount = compute_amount_from_notional(hl_symbol, float(notional))
    else:
        # fallback to default notional
        amount = compute_amount_from_notional(hl_symbol, float(DEFAULT_NOTIONAL_USD))

    # Sanity
    if amount is None or amount <= 0:
        return jsonify({"status": "error", "message": "amount is zero/invalid"}), 400

    # If user sent a limit price, place a limit. Otherwise market with slippage.
    try:
        order = place_order(hl_symbol, side, amount, limit_px, params)
        return jsonify({
            "status": "success",
            "message": f"{side.upper()} {amount:g} {hl_symbol} "
                       f"{'@ '+str(limit_px) if limit_px else '(market)'}",
            "order": order
        })
    except Exception as e:
        log.error("Exchange error", exc_info=True)
        # CCXT Hyperliquid often returns plain string bodies; mirror them
        msg = str(e)
        return jsonify({"status": "error", "message": f"hyperliquid {msg}"}), 400

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    # For local runs; Render will use Gunicorn
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
