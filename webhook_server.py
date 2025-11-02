# webhook_server.py
import os
import math
import json
import time
import logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ── ENV / CONFIG ────────────────────────────────────────────────────────────────
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()            # "testnet" | "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()             # 0x...
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()           # 0x... (64 hex)
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "GTC").upper()
DEFAULT_LEVERAGE = float(os.getenv("HL_DEFAULT_LEVERAGE", "20"))

_ex: Optional[ccxt.Exchange] = None


def ex() -> ccxt.Exchange:
    """ccxt hyperliquid singleton (configured for sandbox when NETWORK=testnet)."""
    global _ex
    if _ex is not None:
        return _ex

    opts = {
        "apiKey": API_WALLET or None,        # API wallet address
        "privateKey": PRIVATE_KEY or None,   # API wallet private key
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
            log.warning("Could not enable sandbox (testnet): %s", e)

    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))

    _ex = hl
    return _ex


# ── SYMBOLS / MARKET UTILS ──────────────────────────────────────────────────────
def normalize_base(symbol_like: str) -> str:
    """
    Accepts 'BTC', 'BTCUSD', 'BTCUSDT', 'ethusd', etc. Returns 'BTC', 'ETH', ...
    """
    s = (symbol_like or "").upper().strip()
    if s.endswith("USDT") or s.endswith("USD"):
        # strip stable suffix to get the base
        for suf in ("USDT", "USD"):
            if s.endswith(suf):
                s = s[: -len(suf)]
                break
    return s


def symbol_to_hl(user_symbol: str) -> str:
    # HL perp symbols in ccxt are 'BASE/USDC:USDC'
    base = normalize_base(user_symbol)
    return f"{base}/USDC:USDC"


def fetch_last(symbol: str) -> float:
    """Get a usable last price; fallback to mid from orderbook."""
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
    """Returns (amount_step, min_amount, price_step)."""
    m = ex().market(symbol)
    amount_step = ((m.get("precision") or {}).get("amount")
                   or m.get("amountPrecision") or 1e-8)
    min_amount = (((m.get("limits") or {}).get("amount") or {}).get("min")
                  or amount_step)
    price_step = ((m.get("precision") or {}).get("price")
                  or m.get("pricePrecision") or 1e-8)
    return float(amount_step), float(min_amount), float(price_step)


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, Dict[str, Any]]:
    """Floors to step, clamps to min, never returns 0 if feasible."""
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
    _, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} "
            f"for {symbol} (min amount {min_amt})."
        )
    return amt, dbg


# ── POSITION / ORDER HELPERS ────────────────────────────────────────────────────
def get_position(symbol: str) -> Dict[str, Any]:
    """
    Returns {'side': 'long'|'short'|None, 'size': float}
    """
    try:
        poss = ex().fetch_positions([symbol])
    except Exception:
        poss = []
    if not poss:
        return {"side": None, "size": 0.0}

    p = poss[0]
    # try common ccxt fields
    amt = 0.0
    for k in ("contracts", "amount", "positionAmt", "size"):
        v = p.get(k)
        if v is not None:
            try:
                amt = float(v)
                break
            except Exception:
                pass

    if amt > 0:
        return {"side": "long", "size": amt}
    if amt < 0:
        return {"side": "short", "size": abs(amt)}
    return {"side": None, "size": 0.0}


def try_set_leverage(symbol: str, lev: float) -> None:
    """
    Best-effort leverage setter. Some ccxt builds support set_leverage; others require
    exchange-specific params. If it fails, we log and continue (orders will still go through).
    """
    try:
        if hasattr(ex(), "set_leverage"):
            ex().set_leverage(lev, symbol)
            return
        # Alt path via endpoint params (driver-specific; wrapped to never break flow)
        ex().privatePostExchange({
            "type": "leverage",  # driver may ignore this; harmless if unsupported
            "data": {"coin": ex().market(symbol)["base"], "leverage": lev}
        })
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)


# ── ROBUST MARKET ORDER & FLIP LOGIC ───────────────────────────────────────────
ORACLE_RETRY_HINTS = ("Price too far from oracle", "could not immediately match")


def robust_market_order(symbol: str, side: str, amount: float,
                        tif: Optional[str], params: Dict[str, Any]):
    """
    Sends a market order with small retries that progressively increase slippage.
    Returns (order, meta) where meta includes attempt info.
    """
    retry_slippages = [
        float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02")) or 0.02,
        0.05, 0.075, 0.10,
    ]
    meta = {"attempts": []}
    for slip in retry_slippages:
        core = dict(params or {})
        if tif:
            core["tif"] = tif
        core["slippage"] = slip
        try:
            ref = fetch_last(symbol)
            order = ex().create_order(symbol, "market", side, float(amount), ref, core)
            meta["attempts"].append({"ok": True, "slippage": slip})
            return order, meta
        except ccxt.BaseError as ce:
            msg = str(ce)
            meta["attempts"].append({"ok": False, "slippage": slip, "error": msg})

            if "open interest" in msg.lower():
                raise
            if "requires \"privateKey\"" in msg or "authentication" in msg.lower():
                raise
            if not any(h in msg for h in ORACLE_RETRY_HINTS):
                raise
            time.sleep(0.2)
    raise ccxt.ExchangeError(f"robust_market_order: exhausted retries. meta={meta}")


def close_if_opposite(symbol: str, desired_side: str, tif: Optional[str]) -> Dict[str, Any]:
    """
    If there is an open position on the opposite side, close it with reduceOnly market.
    Waits briefly until position is flat.
    """
    pos = get_position(symbol)
    if pos["side"] is None or pos["size"] <= 0:
        return {"closed": False}

    if (pos["side"] == "long" and desired_side == "buy") or \
       (pos["side"] == "short" and desired_side == "sell"):
        return {"closed": False, "reason": "already_aligned"}

    close_side = "sell" if pos["side"] == "long" else "buy"
    params = {"reduceOnly": True}
    order, meta = robust_market_order(symbol, close_side, pos["size"], tif, params)

    # Wait until fully flat (or timeout ~1.5s)
    deadline = time.time() + 1.5
    while time.time() < deadline:
        cur = get_position(symbol)
        if cur["side"] is None or cur["size"] == 0:
            break
        time.sleep(0.15)

    return {"closed": True, "order": order, "meta": meta}


def flip_and_open(symbol: str, desired_side: str, amount: float,
                  tif: Optional[str], params: Dict[str, Any]):
    """
    Ensures single active direction per symbol: close opposite, then open desired.
    Includes tiny pause and robust market retries on the open.
    """
    closed = close_if_opposite(symbol, desired_side, tif)
    time.sleep(0.15)  # tiny settle pause
    order, meta = robust_market_order(symbol, desired_side, amount, tif, params or {})
    return {"closed": closed, "opened": {"order": order, "meta": meta}}


# ── FLASK APP / ROUTES ─────────────────────────────────────────────────────────
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
    ok_creds = bool(API_WALLET and PRIVATE_KEY)
    bal = None
    try:
        bal = ex().fetch_balance().get("USDC", {}).get("free")
    except Exception:
        pass
    return jsonify({
        "status": "healthy",
        "network": NETWORK,
        "credentials_set": ok_creds,
        "trading": "active",
        "balance": bal
    })


@app.post("/webhook/tradingview")
def tradingview():
    """
    Body:
    {
      "symbol": "SOL" | "SOLUSDT" | "BTCUSD" ... ,   # required
      "action": "buy"|"sell",                        # required
      "quantity": 1.0,                               # OR
      "notional": 50,                                # one of these
      "tif": "IOC"|"GTC",                            # optional (defaults HL_DEFAULT_TIF)
      "leverage": 20,                                # optional (defaults HL_DEFAULT_LEVERAGE)
      "reduce_only": true,                           # optional (usually for manual closes)
      "price": 180.25                                # optional (limit path; not used here)
    }
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        base_in = (payload.get("symbol") or "").strip()
        if not base_in:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        hl_symbol = symbol_to_hl(base_in)
        log.info("Resolved symbol '%s' -> '%s'", base_in, hl_symbol)

        # Ensure market exists
        try:
            ex().market(hl_symbol)
        except Exception:
            return jsonify({"status": "error", "message": f"Unknown or unsupported market {hl_symbol}"}), 400

        # Leverage (best-effort)
        lev = float(payload.get("leverage", DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE)
        try_set_leverage(hl_symbol, lev)

        tif = (payload.get("tif") or DEFAULT_TIF).upper()

        qty = payload.get("quantity")
        notional = payload.get("notional")
        debug_info: Dict[str, Any] = {}

        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
        elif notional is not None:
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["amount_from_notional"] = dbg
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        params: Dict[str, Any] = {}
        if payload.get("reduce_only") is True:
            params["reduceOnly"] = True

        # single-direction policy: close opposite then open desired (with retries)
        result = flip_and_open(hl_symbol, action, amt, tif, params)

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "side": action,
            "amount": float(amt),
            "flip": result["closed"],                  # info about close step
            "order": result["opened"]["order"],       # open order payload
            "retries": result["opened"]["meta"]["attempts"],
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
