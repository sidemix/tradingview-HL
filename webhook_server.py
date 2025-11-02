# webhook_server.py
import os
import re
import math
import time
import json
import logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

# --------------------------
# Logging
# --------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("webhook")

# --------------------------
# ENV / CONFIG
# --------------------------
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()  # "testnet" or "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()   # 0x...
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip() # 0x... (64 hex)

DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "GTC").upper()            # GTC/IOC/FOK
DEFAULT_NOTIONAL = float(os.getenv("HL_DEFAULT_NOTIONAL", "50"))    # $50 per trade
DEFAULT_LEVERAGE = float(os.getenv("HL_DEFAULT_LEVERAGE", "10"))    # default 10x

# Optional allowlist: "BTC,ETH,SOL"
ONLY_EXECUTE = {
    s.strip().upper()
    for s in os.getenv("HL_ONLY_EXECUTE_SYMBOLS", "").split(",")
    if s.strip()
}

# Internal ccxt instance
_ex = None


def ex() -> ccxt.Exchange:
    """Singleton ccxt.hyperliquid with markets loaded."""
    global _ex
    if _ex is not None:
        return _ex

    opts = {
        "apiKey": API_WALLET or None,         # API wallet (some ccxt builds read this)
        "privateKey": PRIVATE_KEY or None,    # API wallet private key
        "walletAddress": API_WALLET or None,  # others read this
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
            log.warning("Could not enable sandbox (testnet): %s", e)

    try:
        hl.load_markets(True)
        log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    except Exception as e:
        log.error("Failed to load markets: %s", e)
        raise

    _ex = hl
    return _ex


# --------------------------
# Market helpers
# --------------------------
SUFFIX_CLEANER = re.compile(r"(USDT|USD|PERP)$", re.IGNORECASE)

def normalize_user_symbol(user_symbol: str) -> Tuple[str, str]:
    """
    Accepts BTC, BTCUSD, BTCUSDT, ETHUSD.P, etc.
    Returns (base, ccxt_symbol) -> ("BTC", "BTC/USDC:USDC")
    """
    base = (user_symbol or "").upper().strip()
    base = SUFFIX_CLEANER.sub("", base)  # strip common suffixes
    base = base.strip(":/-._ ")          # extra cleanup
    hl_symbol = f"{base}/USDC:USDC"
    log.info(f"Resolved symbol '{user_symbol}' -> '{hl_symbol}'")
    return base, hl_symbol


def fetch_last(symbol: str) -> float:
    """Robust last price (fallback to OB midpoint)."""
    try:
        t = ex().fetch_ticker(symbol)
        px = t.get("last") or t.get("close")
        if px:
            return float(px)
    except Exception:
        pass

    ob = ex().fetch_order_book(symbol, limit=5)
    bid = ob["bids"][0][0] if ob.get("bids") else None
    ask = ob["asks"][0][0] if ob.get("asks") else None
    if bid and ask:
        return float((bid + ask) / 2)

    raise RuntimeError(f"Could not fetch last price for {symbol}")


def market_meta(symbol: str) -> Tuple[float, float, float]:
    """
    Returns (amount_step, min_amount, price_step)
    Falls back to sensible tiny steps if missing.
    """
    m = ex().market(symbol)
    amount_step = (
        (m.get("precision") or {}).get("amount")
        or m.get("amountPrecision")
        or 0.00000001
    )
    min_amount = (
        ((m.get("limits") or {}).get("amount") or {}).get("min")
        or amount_step
    )
    price_step = (
        (m.get("precision") or {}).get("price")
        or m.get("pricePrecision")
        or 0.00000001
    )
    return float(amount_step), float(min_amount), float(price_step)


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, Dict[str, Any]]:
    """
    Floors amount to exchange step, bumps to at least min amount, never returns 0.
    """
    amount_step, min_amount, _ = market_meta(symbol)
    floored = floor_to_step(raw_amount, amount_step)
    debug = {
        "raw_amount": raw_amount,
        "amount_step": amount_step,
        "min_amount": min_amount,
        "floored": floored,
    }

    if floored <= 0:
        floored = amount_step
    if floored < min_amount:
        floored = min_amount

    final_amt = float(ex().amount_to_precision(symbol, floored))
    debug["final_amt"] = final_amt
    return final_amt, debug


def compute_amount_from_notional(symbol: str, notional: float) -> Tuple[float, Dict[str, Any]]:
    px = fetch_last(symbol)
    raw_amt = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw_amt)
    dbg.update({"notional": notional, "last_price": px})
    # sanity: cover minimum notional
    _, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} "
            f"for {symbol} (min amount {min_amt})."
        )
    return amt, dbg


# --------------------------
# Order helpers
# --------------------------
def robust_market_order(symbol: str, side: str, amount: float, tif: Optional[str], params: dict):
    """
    Market order with reference price and slippage.
    Retries a couple of times if HL complains (e.g., oracle distance).
    """
    core = dict(params or {})
    if tif:
        core["tif"] = tif

    last = fetch_last(symbol)
    attempt = 0
    last_exc = None
    while attempt < 3:
        try:
            order = ex().create_order(
                symbol,
                "market",
                side,
                float(amount),
                last,  # reference price for HL slippage calc
                {**core, "slippage": DEFAULT_SLIPPAGE},
            )
            return order, {"attempts": attempt + 1, "ref_price": last}
        except ccxt.BaseError as e:
            msg = str(e)
            last_exc = e
            # A couple of helpful nudges
            if "Price too far from oracle" in msg:
                # refresh price and retry
                last = fetch_last(symbol)
                time.sleep(0.15)
            elif "open interest is at cap" in msg.lower():
                # nothing we can do; bubble up
                raise
            else:
                # brief backoff + retry
                time.sleep(0.15)
        attempt += 1
    raise last_exc or RuntimeError("Failed to submit market order after retries")


def try_set_leverage(symbol: str, leverage: float):
    """
    Best-effort leverage setter. Hyperliquid testnet may reject with 422 while still allowing trades.
    """
    try:
        # Many ccxt drivers expose set_leverage(symbol, leverage)
        # If not, this will raise / no-op.
        if hasattr(ex(), "set_leverage"):
            ex().set_leverage(leverage, symbol=symbol, params={})
        else:
            # Some builds require different signature; ignore if not supported.
            pass
    except Exception as e:
        log.warning(f"set_leverage failed for {symbol}: {e} (continuing)")


# --------------------------
# Position helpers (hardened)
# --------------------------
def get_position(symbol: str) -> Dict[str, Any]:
    """
    Returns {'side': 'long'|'short'|None, 'size': float} for Hyperliquid.
    Parses both ccxt standard fields and raw `info`.
    """
    try:
        positions = ex().fetch_positions([symbol])
    except Exception:
        positions = []

    if not positions:
        return {"side": None, "size": 0.0}

    p = positions[0]
    side = None
    size = 0.0

    # ccxt-standard attempt
    try:
        amt = float(p.get("contracts") or p.get("amount") or 0)
        if amt > 0:
            side, size = "long", amt
        elif amt < 0:
            side, size = "short", abs(amt)
    except Exception:
        pass

    # fallback to hyperliquid info structure
    info = p.get("info") or {}
    try:
        pos_obj = info.get("position") or {}
        position_size = float(pos_obj.get("size", 0))
        if position_size > 0:
            side, size = "long", position_size
        elif position_size < 0:
            side, size = "short", abs(position_size)
    except Exception:
        pass

    return {"side": side, "size": size}


def close_if_opposite(symbol: str, desired_side: str, tif: Optional[str]) -> Dict[str, Any]:
    """
    If an opposite position exists, close it with reduceOnly market.
    Waits briefly until flat before returning.
    """
    pos = get_position(symbol)
    if not pos["side"]:
        return {"closed": False, "reason": "no_position"}

    # Already aligned? nothing to do
    if (pos["side"] == "long" and desired_side == "buy") or \
       (pos["side"] == "short" and desired_side == "sell"):
        return {"closed": False, "reason": "already_aligned"}

    close_side = "sell" if pos["side"] == "long" else "buy"
    params = {"reduceOnly": True}

    log.info(f"Closing {pos['side']} position before flipping to {desired_side} on {symbol}")
    order, meta = robust_market_order(symbol, close_side, pos["size"], tif, params)

    # Confirm flat (up to ~3s)
    for _ in range(15):
        cur = get_position(symbol)
        if not cur["side"]:
            log.info(f"{symbol} flat confirmed, proceeding to open new {desired_side} position")
            return {"closed": True, "order": order, "meta": meta}
        time.sleep(0.2)

    log.warning(f"{symbol} still not flat after close attempt; continuing cautiously.")
    return {"closed": True, "order": order, "meta": meta, "warning": "not_flat"}


def flip_and_open(symbol: str, desired_side: str, amount: float, tif: Optional[str], params: dict):
    """
    Enforces single-direction per symbol:
    1) Close opposite side (reduceOnly market), confirm flat
    2) Open desired side (market)
    """
    closed = close_if_opposite(symbol, desired_side, tif)

    # small additional wait if still not flat
    for _ in range(10):
        cur = get_position(symbol)
        if not cur["side"]:
            break
        time.sleep(0.2)

    log.info(f"Opening new {desired_side} position on {symbol} for {amount} units")
    order, meta = robust_market_order(symbol, desired_side, amount, tif, params or {})
    return {"closed": closed, "opened": {"order": order, "meta": meta}}


# --------------------------
# Flask app
# --------------------------
app = Flask(__name__)


@app.get("/")
def root():
    return jsonify({
        "status": "ok",
        "network": NETWORK,
        "whoami": "/whoami",
        "health": "/health",
        "markets": "/markets?base=SOL or /markets?symbol=SOL/USDC:USDC",
        "webhook_example": "/webhook/tradingview"
    })


@app.get("/whoami")
def whoami():
    creds = getattr(ex(), "requiredCredentials", None)
    return jsonify({
        "network": NETWORK,
        "apiWallet_env": API_WALLET,
        "ownerWallet": API_WALLET,
        "privateKey_present": bool(PRIVATE_KEY),
        "ccxt_required": creds,
    })


@app.get("/markets")
def markets():
    base = request.args.get("base")
    sym = request.args.get("symbol")
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


@app.get("/health")
def health():
    try:
        bal = None
        try:
            bal = ex().fetch_balance().get("USDC", {}).get("free")
        except Exception:
            pass
        return jsonify({
            "status": "healthy",
            "network": NETWORK,
            "credentials_set": bool(API_WALLET and PRIVATE_KEY),
            "trading": "active",
            "balance": bal
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.post("/webhook/tradingview")
def tradingview():
    """
    Body (TradingView alert message JSON):
    {
      "symbol": "BTC" | "BTCUSD" | "BTCUSDT",
      "action": "buy" | "sell",
      "quantity": 0.01,        # optional (units)
      "notional": 50,          # optional ($); used if 'quantity' is absent
      "leverage": 20,          # optional (best effort)
      "tif": "IOC" | "GTC"     # optional; defaults to HL_DEFAULT_TIF
    }
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        user_sym = (payload.get("symbol") or "").strip()
        if not user_sym:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        base, hl_symbol = normalize_user_symbol(user_sym)

        if ONLY_EXECUTE and base not in ONLY_EXECUTE:
            return jsonify({"status": "skipped", "message": f"{base} not in allowlist"}), 200

        # Ensure market exists
        ex().market(hl_symbol)

        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        leverage = float(payload.get("leverage", DEFAULT_LEVERAGE))

        qty = payload.get("quantity")
        notional = payload.get("notional", None if qty is not None else DEFAULT_NOTIONAL)

        # Amount calculation
        debug_info = {}
        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
        elif notional is not None:
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["amount_from_notional"] = dbg
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        # Try set leverage (non-fatal)
        try_set_leverage(hl_symbol, leverage)

        # Enforce flip (close opposite, then open)
        result = flip_and_open(hl_symbol, action, amt, tif, params={})

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "base": base,
            "side": action,
            "amount": float(amt),
            "leverage_requested": leverage,
            "tif": tif,
            "result": result,
            "debug": debug_info
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
