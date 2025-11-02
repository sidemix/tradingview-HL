# webhook_server.py
import os, math, time, logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ----------------- ENV / CONFIG -----------------
NETWORK             = os.getenv("HL_NETWORK", "testnet").lower()       # "testnet" | "mainnet"
API_WALLET          = (os.getenv("HL_API_WALLET") or "").strip()
PRIVATE_KEY         = (os.getenv("HL_PRIVATE_KEY") or "").strip()
DEFAULT_SLIPPAGE    = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF         = os.getenv("HL_DEFAULT_TIF", "IOC").upper()
DEFAULT_NOTIONAL    = float(os.getenv("HL_DEFAULT_NOTIONAL", "50"))    # fallback

# How long to wait (and poll) after a reduceOnly close before opening the flip
FLAT_WAIT_SECS      = float(os.getenv("HL_FLAT_WAIT_SECS", "0.8"))
FLAT_RETRY          = int(os.getenv("HL_FLAT_RETRY", "4"))

_ex = None

def ex() -> ccxt.Exchange:
    global _ex
    if _ex: return _ex

    opts = {
        "apiKey": API_WALLET or None,
        "privateKey": PRIVATE_KEY or None,
        "walletAddress": API_WALLET or None,
        "options": {
            "defaultSlippage": DEFAULT_SLIPPAGE,
        },
    }
    hl = ccxt.hyperliquid(opts)

    if NETWORK == "testnet":
        try:
            hl.set_sandbox_mode(True)
            log.info("✅ ccxt Hyperliquid sandbox (testnet) enabled")
        except Exception as e:
            log.warning("Could not enable sandbox: %s", e)

    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    _ex = hl
    return _ex

# ----------------- SYMBOL HELPERS -----------------
def normalize_base(b: str) -> str:
    """Return base like BTC, SOL, DOGE from many alert spellings."""
    b = (b or "").upper().strip()
    for suf in ("USDT.P", "USDT", "USDTP", "USD", "PERP"):
        if b.endswith(suf):
            b = b[: -len(suf)]
            break
    return b

def symbol_to_hl(user_symbol: str) -> str:
    base = normalize_base(user_symbol)
    return f"{base}/USDC:USDC"

# ----------------- PRECISION / PRICES -----------------
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
    if bid and ask:
        return float((bid + ask) / 2.0)
    raise RuntimeError(f"Could not fetch last price for {symbol}")

def market_meta(symbol: str) -> Tuple[float, float, float]:
    m = ex().market(symbol)
    amount_step = ((m.get("precision") or {}).get("amount")
                   or m.get("amountPrecision") or 1e-8)
    min_amount  = (((m.get("limits") or {}).get("amount") or {}).get("min")
                   or amount_step)
    price_step  = ((m.get("precision") or {}).get("price")
                   or m.get("pricePrecision") or 1e-8)
    return float(amount_step), float(min_amount), float(price_step)

def floor_to_step(v: float, step: float) -> float:
    if step <= 0: return v
    return math.floor(v / step) * step

def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, Dict[str, Any]]:
    step, min_amt, _ = market_meta(symbol)
    floored = floor_to_step(raw_amount, step)
    if floored <= 0: floored = step
    if floored < min_amt: floored = min_amt
    final_amt = float(ex().amount_to_precision(symbol, floored))
    return final_amt, {"raw_amount": raw_amount, "amount_step": step,
                       "min_amount": min_amt, "floored": floored, "final_amt": final_amt}

def amount_from_notional(symbol: str, notional: float) -> Tuple[float, Dict[str, Any]]:
    px = fetch_last(symbol)
    raw = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw)
    dbg.update({"notional": notional, "last_price": px})
    # sanity against min notional
    _, min_amt, _ = market_meta(symbol)
    if amt < min_amt:
        raise ValueError(f"Notional ${notional:.2f} too small; min ~{min_amt*px:.2f}")
    return amt, dbg

# ----------------- POSITIONS -----------------
def get_position(symbol: str) -> Dict[str, Any]:
    """Return a compact position dict for symbol; zeroed if flat."""
    try:
        positions = ex().fetch_positions([symbol])
    except Exception:
        positions = ex().fetch_positions()

    pos = None
    for p in (positions or []):
        if p.get("symbol") == symbol:
            pos = p
            break

    if not pos:
        return {"size": 0.0, "side": "none", "symbol": symbol}

    # ccxt unified: p["contracts"] is abs size; p["side"] in {"long","short","none"}
    size = float(pos.get("contracts") or pos.get("contractSize") or pos.get("size") or 0.0)
    side = pos.get("side") or ("none" if size == 0 else "long")  # best effort
    # In HL, size is positive; rely on 'side'
    if side not in ("long", "short"): side = "none"
    return {"size": size, "side": side, "symbol": symbol, "raw": pos}

def place_market(symbol: str, side: str, amount: float, tif: Optional[str], extra: dict = None):
    ref_price = fetch_last(symbol)
    params = {"slippage": DEFAULT_SLIPPAGE}
    if tif: params["tif"] = tif
    if extra: params.update(extra)
    return ex().create_order(symbol, "market", side, float(amount), ref_price, params)

# ----------------- FLIP LOGIC -----------------
def flip_or_scale(symbol: str, desired_side: str, target_amt: float, tif: str) -> Dict[str, Any]:
    """
    Ensure the market ends with:
      - a position on desired_side of size ~= target_amt (within one step)
    """
    pos = get_position(symbol)
    step, _, _ = market_meta(symbol)
    log.info("Current pos %s: side=%s size=%.8f (step=%g)", symbol, pos["side"], pos["size"], step)

    # If flat -> just open target
    if pos["side"] == "none" or pos["size"] <= (step * 0.5):
        log.info("Opening new %s position on %s for %.8f units", desired_side, symbol, target_amt)
        ord1 = place_market(symbol, desired_side, target_amt, tif)
        return {"did": "open", "open": ord1}

    # Same side -> scale to target (only trade the delta)
    if pos["side"] == desired_side:
        delta = target_amt - pos["size"]
        # If within one step, no-op
        if abs(delta) < step:
            log.info("Same side & already at target (|delta| < step). No trade.")
            return {"did": "noop", "reason": "at_target"}
        side = desired_side if delta > 0 else ("sell" if desired_side == "long" else "buy")
        amt = abs(delta)
        log.info("Same side; scaling %s by %.8f", side, amt)
        ord1 = place_market(symbol, side, amt, tif)
        return {"did": "scale", "delta": delta, "order": ord1}

    # Opposite side -> full flip: reduceOnly to flat, wait, then open new side
    # First, flatten existing fully
    close_side = "sell" if pos["side"] == "long" else "buy"
    log.info("Opposite side; flattening %s %.8f with reduceOnly", symbol, pos["size"])
    _ = place_market(symbol, close_side, pos["size"], "IOC", {"reduceOnly": True})

    # Poll until flat (briefly)
    for i in range(FLAT_RETRY):
        time.sleep(FLAT_WAIT_SECS / max(1, FLAT_RETRY))
        again = get_position(symbol)
        if again["side"] == "none" or again["size"] <= (step * 0.5):
            break

    # Now open the new desired side at the target amount
    open_side = "buy" if desired_side == "long" else "sell"
    log.info("Opening flipped %s on %s for %.8f units", open_side, symbol, target_amt)
    ord2 = place_market(symbol, open_side, target_amt, tif)
    return {"did": "flip", "closedSize": pos["size"], "open": ord2}

# ----------------- (Optional) leverage helper -----------------
def try_set_leverage(symbol: str, lev: Optional[float]):
    if not lev: return
    try:
        # ccxt hyperliquid supports set_leverage? Some builds do, some don't.
        # We call and ignore failures (esp. testnet 422).
        ex().set_leverage(lev, symbol)
        log.info("Leverage set %sx for %s", lev, symbol)
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)

# ----------------- FLASK APP -----------------
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
    req = getattr(ex(), "requiredCredentials", None)
    return jsonify({
        "network": NETWORK,
        "apiWallet_env": API_WALLET,
        "ownerWallet": API_WALLET,
        "privateKey_present": bool(PRIVATE_KEY),
        "ccxt_required": req,
    })

@app.get("/health")
def health():
    ok_creds = bool(API_WALLET and PRIVATE_KEY)
    bal = None
    try:
        bal = ex().fetch_balance().get("USDC", {}).get("free")
    except Exception:
        pass
    return jsonify({"status": "healthy", "network": NETWORK,
                    "credentials_set": ok_creds, "balance": bal})

@app.get("/markets")
def markets():
    base = request.args.get("base")
    sym  = request.args.get("symbol")
    data = []
    if sym:
        m = ex().market(sym)
        data.append({
            "symbol": m["symbol"],
            "base": m.get("base"),
            "quote": m.get("quote"),
            "settle": m.get("settle"),
            "amountPrecision": (m.get("precision") or {}).get("amount") or m.get("amountPrecision"),
            "pricePrecision": (m.get("precision") or {}).get("price") or m.get("pricePrecision"),
            "limits": m.get("limits"),
        })
    else:
        for m in ex().markets.values():
            if (not base) or (m.get("base") == base.upper()):
                data.append({
                    "symbol": m["symbol"],
                    "base": m.get("base"),
                    "quote": m.get("quote"),
                    "settle": m.get("settle"),
                    "amountPrecision": (m.get("precision") or {}).get("amount") or m.get("amountPrecision"),
                    "pricePrecision": (m.get("precision") or {}).get("price") or m.get("pricePrecision"),
                })
    return jsonify({"count": len(data), "markets": data})

@app.post("/webhook/tradingview")
def tradingview():
    """
    Body (from TradingView alert message):
    {
      "symbol": "BTC", "action": "buy" | "sell",
      "quantity": 0.5,                 # OR
      "notional": 50,                  # $ notional
      "leverage": 20,                  # optional (best-effort)
      "tif": "IOC" | "GTC"
    }
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        base = payload.get("symbol")
        if not base:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        hl_symbol = symbol_to_hl(base)
        log.info("Resolved symbol '%s' -> '%s'", base, hl_symbol)
        ex().market(hl_symbol)  # validate

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be buy or sell"}), 400

        desired_side = "long" if action == "buy" else "short"
        tif = (payload.get("tif") or DEFAULT_TIF).upper()

        # Best-effort leverage
        lev = None
        try:
            lev = float(payload.get("leverage")) if payload.get("leverage") is not None else None
        except Exception:
            lev = None
        try_set_leverage(hl_symbol, lev)

        # Amount calc
        debug = {}
        if payload.get("quantity") is not None:
            amt, dbg = clamp_amount(hl_symbol, float(payload["quantity"]))
            debug["amount_from_quantity"] = dbg
        else:
            notional = float(payload.get("notional") or DEFAULT_NOTIONAL)
            amt, dbg = amount_from_notional(hl_symbol, notional)
            debug["amount_from_notional"] = dbg

        result = flip_or_scale(hl_symbol, desired_side, amt, tif)

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "requested_side": desired_side,
            "target_amount": amt,
            "result": result,
            "debug": debug
        })

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
