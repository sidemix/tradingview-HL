import os, json, logging
from decimal import Decimal, InvalidOperation
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt
from eth_account import Account

# ----------------------------- bootstrap ---------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("webhook")

USE_TESTNET = os.getenv("USE_TESTNET", "true").strip().lower() == "true"

OWNER_ADDR   = (os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
API_ADDR_ENV = (os.getenv("HYPERLIQUID_API_WALLET_ADDRESS") or "").strip()
PRIV         = (os.getenv("HYPERLIQUID_SECRET_KEY") or "").strip()

DEFAULT_NOTIONAL_USD = float(os.getenv("DEFAULT_NOTIONAL_USD", "50"))
DEFAULT_SLIPPAGE     = float(os.getenv("DEFAULT_SLIPPAGE", "0.02"))  # 2%
ALLOWED_SYMBOLS = [s.strip().upper() for s in os.getenv(
    "ALLOWED_SYMBOLS", "BTC,ETH,SOL,LINK,BNB,AVAX"
).split(",") if s.strip()]

def _normalize_hex(x: str) -> str:
    x = (x or "").strip()
    return x if x.startswith("0x") else ("0x" + x)

def _safe_float(x, default=None):
    try:
        if isinstance(x, (int, float)): return float(x)
        if isinstance(x, Decimal):      return float(x)
        if isinstance(x, str):          return float(x.strip())
        return default
    except (ValueError, InvalidOperation, TypeError):
        return default

def build_urls():
    host = "hyperliquid-testnet.xyz" if USE_TESTNET else "hyperliquid.xyz"
    base = f"https://api.{host}"
    return {"api": {"public": base, "private": base}}

def derive_addr_from_priv(priv_hex: str) -> str:
    return Account.from_key(_normalize_hex(priv_hex)).address

# env validation
if not OWNER_ADDR:   raise RuntimeError("HYPERLIQUID_ACCOUNT_ADDRESS not set")
if not API_ADDR_ENV: raise RuntimeError("HYPERLIQUID_API_WALLET_ADDRESS not set")
if not PRIV:         raise RuntimeError("HYPERLIQUID_SECRET_KEY not set")

DERIVED_ADDR = derive_addr_from_priv(PRIV)

log.info("Network: %s", "testnet" if USE_TESTNET else "mainnet")
log.info("Owner (account)     : %s", OWNER_ADDR)
log.info("API wallet (env)    : %s", API_ADDR_ENV)
log.info("API wallet (derived): %s", DERIVED_ADDR)

if DERIVED_ADDR.lower() != API_ADDR_ENV.lower():
    raise RuntimeError(
        "API wallet mismatch:\n"
        f"  derived from PRIVATE KEY: {DERIVED_ADDR}\n"
        f"  env API wallet address  : {API_ADDR_ENV}\n"
        "Fix your key/address pair so they match the authorized API wallet."
    )

# ------------------------------- ccxt ------------------------------------
def make_exchange():
    ex = ccxt.hyperliquid({
        # signer (API wallet)
        "walletAddress": API_ADDR_ENV,
        "privateKey": _normalize_hex(PRIV),

        # explicit owner + signer in options
        "options": {
            "defaultType": "swap",
            "defaultSlippage": DEFAULT_SLIPPAGE,
            "apiWalletAddress": API_ADDR_ENV,   # signer
            "accountAddress":   OWNER_ADDR,     # owner account
        },
        "urls": build_urls(),
    })
    ex.load_markets()
    log.info("✅ Connected to Hyperliquid via CCXT (%s)", "testnet" if USE_TESTNET else "mainnet")
    return ex

ex = make_exchange()

# ---------------------------- market helpers -----------------------------
def resolve_market_symbol(base: str) -> str:
    b = base.upper().strip()
    candidates = [
        f"{b}/USDC:USDC", f"{b}/USD:USD", f"{b}/USDC", f"{b}/USD",
        f"{b}-PERP", f"{b}USDC", f"{b}USD",
    ]
    for s in candidates:
        if s in ex.markets:
            return s
    # any swap with matching base
    for m in ex.markets.values():
        if (m.get("base") or "").upper() == b and m.get("swap"):
            return m["symbol"]
    # last resort
    for m in ex.markets.values():
        sym = (m.get("symbol") or "").upper()
        if sym.startswith(b) and m.get("swap"):
            return m["symbol"]
    raise RuntimeError(f"No Hyperliquid market found for base={b}")

def fetch_last(symbol: str) -> float:
    t = ex.fetch_ticker(symbol)
    last = _safe_float(t.get("last"))
    if last and last > 0:
        return last
    info = t.get("info") or {}
    fallbacks = [
        t.get("mark"), t.get("ask"), t.get("bid"),
        info.get("markPx"), info.get("oraclePx"),
        info.get("mid"), info.get("p"), info.get("last"),
    ]
    for v in fallbacks:
        vv = _safe_float(v)
        if vv and vv > 0:
            return vv
    raise RuntimeError(f"Could not fetch last price for {symbol}")

def compute_amount_from_notional(symbol: str, notional_usd: float) -> float:
    px = fetch_last(symbol)
    return float(notional_usd) / float(px)

def pinned_params(extra: dict | None = None) -> dict:
    # FORCE the signer identity on every call
    p = {
        "vaultAddress": API_ADDR_ENV,
        "walletAddress": API_ADDR_ENV,
        "accountAddress": OWNER_ADDR,
    }
    if extra:
        p.update(extra)
    return p

def place_order(symbol: str, side: str, amount: float, limit_price: float | None, params: dict):
    side = side.lower()
    core = pinned_params(params)
    if limit_price is not None:
        return ex.create_order(symbol, "limit", side, float(amount), float(limit_price), core)
    ref_price = fetch_last(symbol)  # give ccxt a price for slippage
    core = pinned_params({**params, "slippage": DEFAULT_SLIPPAGE})
    return ex.create_order(symbol, "market", side, float(amount), ref_price, core)

# -------------------------------- Flask ----------------------------------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({
        "message": "TradingView → Hyperliquid webhook (CCXT)",
        "endpoints": {"health": "/health", "whoami": "/whoami", "markets": "/markets", "webhook": "/webhook/tradingview"},
        "network": "testnet" if USE_TESTNET else "mainnet",
        "allowed_symbols": ALLOWED_SYMBOLS
    })

@app.get("/health")
def health():
    bal = None
    try:
        b = ex.fetch_balance()
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

@app.get("/markets")
def markets():
    # quick peek to verify symbol names HL expects on your ccxt version
    out = []
    for k, m in ex.markets.items():
        if m.get("swap"):
            out.append({"symbol": k, "base": m.get("base"), "quote": m.get("quote")})
    return jsonify(out[:100])

@app.post("/webhook/tradingview")
def tradingview():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    log.info("Received alert: %s", data)

    symbol_in = (data.get("symbol") or "").upper().strip()
    action    = (data.get("action") or "").lower().strip()
    quantity  = _safe_float(data.get("quantity"))
    notional  = _safe_float(data.get("notional"))
    tif       = (data.get("tif") or "GTC").upper().strip()
    post_only   = bool(data.get("post_only", False))
    reduce_only = bool(data.get("reduce_only", False))
    limit_px    = _safe_float(data.get("price"))

    if not symbol_in:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    base = symbol_in.replace("/USD", "").replace("/USDC", "").replace("USDT", "").strip()
    if ALLOWED_SYMBOLS and base not in ALLOWED_SYMBOLS:
        return jsonify({"status": "error", "message": f"symbol {base} not allowed"}), 400

    side = "sell" if action in ("sell", "short") else "buy"
    if side not in ("buy", "sell"):
        return jsonify({"status": "error", "message": "action must be buy/sell (or long/short)"}), 400

    try:
        hl_symbol = resolve_market_symbol(base)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    params = {}
    if tif in ("IOC", "GTC"):
        params["timeInForce"] = tif
    if post_only:
        params["postOnly"] = True
    if reduce_only:
        params["reduceOnly"] = True

    if quantity is not None and quantity > 0:
        amount = float(quantity)
    else:
        amount = compute_amount_from_notional(hl_symbol, float(notional) if notional else float(DEFAULT_NOTIONAL_USD))

    if amount <= 0:
        return jsonify({"status": "error", "message": "amount is zero/invalid"}), 400

    try:
        order = place_order(hl_symbol, side, amount, limit_px, params)
        return jsonify({
            "status": "success",
            "message": f"{side.upper()} {amount:g} {hl_symbol} "
                       f"{'@ '+str(limit_px) if limit_px else '(market with ref price)'}",
            "order": order
        })
    except Exception as e:
        log.error("Exchange error", exc_info=True)
        return jsonify({"status": "error", "message": f"hyperliquid {str(e)}"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
