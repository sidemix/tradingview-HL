# webhook_server.py
import os, math, json, logging, re
from typing import Optional, Tuple
from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ---- ENV / CONFIG ----
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()
API_WALLET = os.getenv("HL_API_WALLET", "").strip()
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "IOC").upper()
DEFAULT_NOTIONAL = float(os.getenv("HL_DEFAULT_NOTIONAL", "50"))
DEFAULT_LEVERAGE = float(os.getenv("HL_DEFAULT_LEVERAGE", "20"))

_ex = None
def ex() -> ccxt.Exchange:
    global _ex
    if _ex: return _ex
    opts = {
        "apiKey": API_WALLET or None,
        "privateKey": PRIVATE_KEY or None,
        "walletAddress": API_WALLET or None,
        "options": {"defaultSlippage": DEFAULT_SLIPPAGE},
    }
    hl = ccxt.hyperliquid(opts)
    if NETWORK == "testnet":
        try:
            hl.set_sandbox_mode(True)
            log.info("✅ ccxt Hyperliquid sandbox (testnet) enabled")
        except Exception as e:
            log.warning("Could not enable sandbox (testnet): %s", e)
    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    _ex = hl
    return _ex

# ---------- symbol normalization ----------
_STABLE_SUFFIXES = ("USDT", "USD", "USDC")
_EXTRA_SUFFIXES = ("PERP", "PERPETUAL")

def _strip_exchange_prefix(sym: str) -> str:
    # e.g., "BINANCE:BTCUSD.P" -> "BTCUSD.P"
    return sym.split(":", 1)[-1]

def _sanitize(sym: str) -> str:
    # remove dots/derivative suffixes like ".P" or "-PERP"
    return sym.replace(".P", "").replace(".p", "").replace("-PERP", "").replace("-perp", "")

def normalize_base(user_symbol: str) -> str:
    """
    Accepts: BTC, BTCUSD, BTCUSDT, BINANCE:ETHUSD, ASTERUSDT.P, etc.
    Returns base like BTC, ETH, ASTER if possible.
    """
    s = _sanitize(_strip_exchange_prefix((user_symbol or "").upper().strip()))
    # strip any trailing "/..." part if present
    s = s.split("/", 1)[0]

    # remove known extra suffix tokens
    for suf in _EXTRA_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]

    # pattern: <BASE><STABLE> (e.g., BTCUSDT, ETHUSD)
    m = re.match(r"^([A-Z0-9]+?)(USDT|USD|USDC)$", s)
    if m:
        return m.group(1)

    # already a clean base (e.g., BTC, SOL, ASTER)
    return s

def resolve_hl_symbol(user_symbol: str) -> str:
    """
    Try several ways to map to a valid HL market, preferring /USDC:USDC.
    """
    base = normalize_base(user_symbol)
    candidate = f"{base}/USDC:USDC"
    # quick success path
    if candidate in ex().markets:
        return candidate

    # Some bases differ slightly; search markets for matching base + USDC settle
    for m in ex().markets.values():
        if m.get("base") == base and m.get("quote") == "USDC" and m.get("settle") == "USDC":
            return m["symbol"]

    # last resort: if user already sent a full HL symbol, just pass it through
    if user_symbol in ex().markets:
        return user_symbol

    raise ccxt.BadSymbol(f"Unknown or unsupported market for '{user_symbol}' (normalized base '{base}')")

# ---------- pricing / sizing ----------
def fetch_last(symbol: str) -> float:
    try:
        t = ex().fetch_ticker(symbol)
        px = t.get("last") or t.get("close")
        if px: return float(px)
    except Exception:
        pass
    ob = ex().fetch_order_book(symbol, limit=5)
    bid = ob["bids"][0][0] if ob.get("bids") else None
    ask = ob["asks"][0][0] if ob.get("asks") else None
    if bid and ask: return float((bid + ask) / 2)
    raise RuntimeError(f"Could not fetch last price for {symbol}")

def market_meta(symbol: str):
    m = ex().market(symbol)
    amount_step = (m.get("precision") or {}).get("amount") or m.get("amountPrecision") or 1e-8
    min_amount = (((m.get("limits") or {}).get("amount") or {}).get("min")) or amount_step
    price_step = (m.get("precision") or {}).get("price") or m.get("pricePrecision") or 1e-8
    return float(amount_step), float(min_amount), float(price_step)

def floor_to_step(value: float, step: float) -> float:
    return value if step <= 0 else math.floor(value / step) * step

def clamp_amount(symbol: str, raw_amount: float):
    step, min_amt, _ = market_meta(symbol)
    floored = floor_to_step(raw_amount, step)
    if floored <= 0: floored = step
    if floored < min_amt: floored = min_amt
    final_amt = float(ex().amount_to_precision(symbol, floored))
    return final_amt, {
        "raw_amount": raw_amount, "amount_step": step, "min_amount": min_amt,
        "floored": floored, "final_amt": final_amt
    }

def compute_amount_from_notional(symbol: str, notional: float):
    px = fetch_last(symbol)
    raw_amt = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw_amt)
    dbg.update({"notional": notional, "last_price": px})
    _, min_amt, _ = market_meta(symbol)
    if amt <= 0 or notional < (min_amt * px):
        raise ValueError(f"Notional ${notional:.2f} below minimum for {symbol}")
    return amt, dbg

def place_order(symbol: str, side: str, amount: float, tif: Optional[str], params: dict):
    ref_price = fetch_last(symbol)
    core = {**(params or {}), "slippage": DEFAULT_SLIPPAGE}
    if tif: core["timeInForce"] = tif
    return ex().create_order(symbol, "market", side, float(amount), ref_price, core)

# ---------- flip helpers ----------
def get_open_position(symbol: str) -> dict:
    try:
        positions = ex().fetch_positions([symbol])
    except Exception:
        positions = [p for p in ex().fetch_positions() if p.get("symbol") == symbol]
    for p in positions:
        if p.get("symbol") != symbol: continue
        side = p.get("side")
        amt = p.get("positionAmt") or p.get("size") or p.get("contracts") or 0
        try: amt = float(amt)
        except Exception: amt = 0.0
        signed = amt if side == "long" else (-amt if side == "short" else 0.0)
        return {"size": signed, "side": side, "entryPrice": float(p.get("entryPrice") or 0)}
    return {"size": 0.0, "side": None, "entryPrice": 0.0}

def close_position(symbol: str, tif: str = DEFAULT_TIF) -> dict:
    pos = get_open_position(symbol)
    size = float(pos["size"])
    if abs(size) <= 0: return {"closed": False, "reason": "flat"}
    step, _, _ = market_meta(symbol)
    amt = floor_to_step(abs(size), step)
    if amt <= 0: return {"closed": False, "reason": "zero_after_floor"}
    side = "sell" if size > 0 else "buy"
    ref_price = fetch_last(symbol)
    params = {"reduceOnly": True, "timeInForce": tif, "slippage": DEFAULT_SLIPPAGE}
    order = ex().create_order(symbol, "market", side, amt, ref_price, params)
    return {"closed": True, "order": order, "sizeClosed": amt, "side": side}

def ensure_leverage(symbol: str, lev: float):
    try:
        if lev: ex().set_leverage(lev, symbol)
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)

# ---------- Flask ----------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({
        "status": "ok",
        "network": NETWORK,
        "whoami": "/whoami",
        "health": "/health",
        "markets": "/markets?base=SOL (or ?symbol=SOL/USDC:USDC)",
        "webhook": "/webhook/tradingview"
    })

@app.get("/whoami")
def whoami():
    req = getattr(ccxt.hyperliquid, "required_credentials", getattr(ex(), "requiredCredentials", {}))
    return jsonify({
        "network": NETWORK,
        "apiWallet_env": API_WALLET.lower(),
        "ownerWallet": API_WALLET.lower(),
        "privateKey_present": bool(PRIVATE_KEY),
        "ccxt_required": req,
    })

@app.get("/markets")
def markets():
    base = request.args.get("base")
    sym = request.args.get("symbol")
    data = []
    if sym:
        m = ex().market(sym)
        data.append({
            "symbol": m["symbol"], "base": m.get("base"), "quote": m.get("quote"),
            "settle": m.get("settle"),
            "amountPrecision": (m.get("precision") or {}).get("amount") or m.get("amountPrecision"),
            "pricePrecision": (m.get("precision") or {}).get("price") or m.get("pricePrecision"),
            "limits": m.get("limits"),
        })
    else:
        for m in ex().markets.values():
            if (not base) or (m.get("base") == base.upper()):
                data.append({
                    "symbol": m["symbol"], "base": m.get("base"), "quote": m.get("quote"),
                    "settle": m.get("settle"),
                    "amountPrecision": (m.get("precision") or {}).get("amount") or m.get("amountPrecision"),
                    "pricePrecision": (m.get("precision") or {}).get("price") or m.get("pricePrecision"),
                })
    return jsonify({"count": len(data), "markets": data})

@app.get("/health")
def health():
    ok_creds = bool(API_WALLET and PRIVATE_KEY)
    bal = None
    try:
        bal = ex().fetch_balance().get("USDC", {}).get("free")
    except Exception:
        pass
    return jsonify({
        "status": "healthy", "network": NETWORK,
        "credentials_set": ok_creds, "trading": "active", "balance": bal
    })

@app.post("/webhook/tradingview")
def tradingview():
    """
    Examples:
      {"symbol":"BTCUSD","action":"buy","notional":50,"leverage":20,"tif":"IOC"}
      {"symbol":"BINANCE:SOLUSDT.P","action":"sell","notional":50}
      {"symbol":"DOGE","action":"buy","quantity":100}
    Flip: closes opposite side before opening the new side.
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        raw_sym = (payload.get("symbol") or "").strip()
        if not raw_sym:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        # <<< New: robust mapping >>>
        hl_symbol = resolve_hl_symbol(raw_sym)
        log.info("Resolved symbol '%s' -> '%s'", raw_sym, hl_symbol)

        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        leverage = float(payload.get("leverage", DEFAULT_LEVERAGE))

        qty = payload.get("quantity")
        notional = payload.get("notional")

        debug_info = {}
        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
        else:
            if notional is None:
                notional = DEFAULT_NOTIONAL
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["amount_from_notional"] = dbg

        if amt <= 0:
            return jsonify({"status": "error", "message": "Computed zero amount", "debug": debug_info}), 400

        ensure_leverage(hl_symbol, leverage)

        # flip if needed
        pos = get_open_position(hl_symbol)
        size = float(pos["size"])
        need_buy = (action == "buy")
        same_dir = (size > 0 and need_buy) or (size < 0 and not need_buy)
        if size != 0 and not same_dir:
            closed = close_position(hl_symbol, tif=tif)
            log.info("Flip: closed %s -> %s", hl_symbol, json.dumps(closed, default=str))

        order = place_order(hl_symbol, action, amt, tif, params={})

        return jsonify({"status": "ok", "symbol": hl_symbol, "side": action, "amount": float(amt),
                        "order": order, "debug": debug_info})

    except ValueError as ve:
        return jsonify({"status": "error", "message": str(ve)}), 400
    except ccxt.BaseError as ce:
        log.exception("Exchange error")
        return jsonify({"status": "error", "message": f"hyperliquid {str(ce)}"}), 400
    except Exception as e:
        log.exception("Unhandled")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
