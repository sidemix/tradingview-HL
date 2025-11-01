import os
import json
import time
import logging
from decimal import Decimal

from flask import Flask, request, jsonify
from dotenv import load_dotenv

# CCXT handles the Hyperliquid signing & exchange payloads for us
import ccxt

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ──────────────────────────────────────────────────────────────────────────────
# Env
# ──────────────────────────────────────────────────────────────────────────────
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
HL_ADDRESS = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
HL_PRIVKEY = os.getenv("HYPERLIQUID_SECRET_KEY", "").strip()  # 0x… private key of API wallet
DEFAULT_NOTIONAL = Decimal(os.getenv("DEFAULT_NOTIONAL_USD", "50"))  # fallback if qty not provided
ALLOWED_SYMBOLS = set([s.strip().upper() for s in os.getenv(
    "ALLOWED_SYMBOLS",
    "BTC,ETH,SOL,LINK,BNB,AVAX"
).split(",") if s.strip()])

if not HL_ADDRESS or not HL_PRIVKEY:
    log.warning("Hyperliquid credentials not set – server will run but trades will be blocked.")

# ──────────────────────────────────────────────────────────────────────────────
# Exchange init (ccxt)
# ──────────────────────────────────────────────────────────────────────────────
def make_exchange():
    # CCXT needs apiKey (public address) + secret (priv key). It handles signing.
    # Use testnet host when requested.
    params = {
        "hostname": "hyperliquid-testnet.xyz" if USE_TESTNET else "hyperliquid.xyz",
    }
    exchange = ccxt.hyperliquid({
        "apiKey": HL_ADDRESS,
        "secret": HL_PRIVKEY,
        "options": {
            # Perps is the default; CCXT maps the correct endpoints.
            "defaultType": "swap",
        },
        "urls": {
            "api": {
                "public": f"https://api.{params['hostname']}",
                "private": f"https://api.{params['hostname']}",
            }
        }
    })
    # Small warm-up to load markets & sanity-check creds
    exchange.load_markets()
    return exchange

try:
    ex = make_exchange()
    log.info(f"✅ Connected to Hyperliquid via CCXT ({'testnet' if USE_TESTNET else 'mainnet'})")
except Exception as e:
    log.exception("Failed to initialize exchange")
    ex = None

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def to_hl_symbol(sym: str) -> str:
    """
    Normalize incoming symbols (e.g., 'BTC', 'BTC/USD', 'btc') to CCXT HL symbol.
    For Hyperliquid perps, CCXT exposes symbols like 'BTC/USDC:USDC' or 'BTC/USD:USD' per version.
    We’ll resolve by base and prefer a swap market.
    """
    s = sym.upper().replace("USDT", "USDC").replace("USD", "USDC").replace("/USDC", "")
    base = s.split("/")[0]
    # Find first swap market matching base (BTC -> BTC/USDC:USDC or similar)
    for mkt in ex.markets.values():
        if mkt.get("type") == "swap" and mkt.get("base") == base:
            return mkt["symbol"]
    raise ValueError(f"No HL swap market found for base '{base}'")

def clamp_sz(symbol: str, sz: Decimal) -> str:
    """Match exchange precision for amount."""
    m = ex.market(symbol)
    amount_prec = m.get("precision", {}).get("amount", None)
    if amount_prec is None:
        return str(sz)
    q = Decimal(10) ** (-amount_prec)
    return str((sz // q) * q)

def compute_market_size(symbol: str, notional: Decimal) -> str:
    """Convert USD notional to size using current mark/last price."""
    ticker = ex.fetch_ticker(symbol)
    px = Decimal(str(ticker.get("last") or ticker.get("info", {}).get("markPx") or ticker["close"]))
    if px <= 0:
        raise ValueError("Invalid price from ticker")
    raw = notional / px
    return clamp_sz(symbol, raw)

def parse_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "t", "yes", "y")

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    mode = "active" if (ex and HL_ADDRESS and HL_PRIVKEY) else "demo"
    try:
        bal = ex.fetch_balance() if ex else {}
        withdrawable = bal.get("USDC", {}).get("free") or bal.get("total", {}).get("USDC")
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
        "endpoints": {
            "health": "GET /health",
            "webhook": "POST /webhook/tradingview"
        },
        "network": "testnet" if USE_TESTNET else "mainnet"
    })

@app.post("/webhook/tradingview")
def tradingview():
    if not ex or not (HL_ADDRESS and HL_PRIVKEY):
        return jsonify({
            "status": "demo",
            "message": "[DEMO] Credentials missing or exchange not ready"
        }), 200

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"status":"error","message":"Invalid or missing JSON"}), 400

    log.info(f"Received alert: {payload}")

    # Accept both simple and rich alerts
    symbol_in = (payload.get("symbol") or payload.get("coin") or "BTC")
    action = (payload.get("action") or payload.get("side") or "buy").lower()
    reduce_only = parse_bool(payload.get("reduce_only") or payload.get("reduceOnly"), False)
    post_only = parse_bool(payload.get("post_only") or payload.get("postOnly"), False)
    tif = (payload.get("tif") or payload.get("time_in_force") or "IOC").upper()  # IOC/GTC
    notional = payload.get("notional")  # optional USD notional
    qty = payload.get("quantity") or payload.get("qty")  # optional size in coin

    base = symbol_in.split(":")[0].split("/")[0].upper()
    if ALLOWED_SYMBOLS and base not in ALLOWED_SYMBOLS:
        return jsonify({"status": "blocked", "message": f"Symbol '{base}' not in ALLOWED_SYMBOLS"}), 403

    try:
        hl_symbol = to_hl_symbol(symbol_in)
    except Exception as e:
        return jsonify({"status":"error","message":f"Symbol resolve failed: {e}"}), 400

    # side/ordertype
    side = "buy" if action in ("buy", "long") else "sell"
    is_market = True

    # determine amount
    try:
        if qty is not None:
            amount = clamp_sz(hl_symbol, Decimal(str(qty)))
        else:
            notional_usd = Decimal(str(notional)) if notional is not None else DEFAULT_NOTIONAL
            amount = compute_market_size(hl_symbol, notional_usd)
    except Exception as e:
        return jsonify({"status":"error","message":f"Size calc failed: {e}"}), 400

    # Build params for HL (reduceOnly, postOnly, TIF)
    params = {
        "reduceOnly": reduce_only,
    }
    if post_only:
        params["postOnly"] = True
        is_market = False  # postOnly implies limit, but we keep px=last with tiny slippage if needed
    if tif in ("IOC", "FOK", "GTC"):
        params["timeInForce"] = tif

    try:
        if is_market:
            # CCXT: create_order(symbol, type, side, amount, price=None, params={})
            # Market order: price=None. HL will use mark/last with internal mechanics.
            order = ex.create_order(hl_symbol, "market", side, float(amount), None, params)
        else:
            # Fallback: emulate postOnly with a near-touch limit (use last price)
            ticker = ex.fetch_ticker(hl_symbol)
            px = Decimal(str(ticker.get("last") or ticker["close"]))
            # Slightly “worse” price to guarantee postOnly stays on book
            bump = Decimal("0.001")
            limit_px = px * (Decimal("0.999") if side == "sell" else Decimal("1.001"))
            order = ex.create_order(hl_symbol, "limit", side, float(amount), float(limit_px), params)

        return jsonify({
            "status": "success",
            "message": f"Executed {side.upper()} {amount} {hl_symbol} ({'market' if is_market else 'limit'})",
            "result": order
        })
    except ccxt.BaseError as e:
        log.exception("Exchange error")
        return jsonify({"status":"error","message":str(e)}), 400
    except Exception as e:
        log.exception("Unhandled error")
        return jsonify({"status":"error","message":str(e)}), 500

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
