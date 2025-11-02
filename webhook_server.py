# webhook_server.py
import os
import math
import json
import logging
from typing import Optional, Tuple

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ---- ENV / CONFIG ----
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()       # "testnet" or "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()        # 0x...
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()      # 0x... (64 hex)
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "IOC").upper()   # IOC is best for flips
DEFAULT_NOTIONAL = float(os.getenv("HL_DEFAULT_NOTIONAL", "50"))
DEFAULT_LEVERAGE = float(os.getenv("HL_DEFAULT_LEVERAGE", "20"))

# ccxt exchange singleton
_ex = None


def ex() -> ccxt.Exchange:
    global _ex
    if _ex is not None:
        return _ex

    # ccxt.hyperliquid expects wallet + privateKey for signing
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
            log.warning("Could not enable sandbox (testnet): %s", e)

    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    _ex = hl
    return _ex


def symbol_to_hl(user_symbol: str) -> str:
    base = user_symbol.strip().upper()
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
    """
    Returns (amount_step, min_amount, price_step)
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


def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, dict]:
    """
    Floors amount to step, clamps to min size, never returns 0 if trade is feasible.
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


def compute_amount_from_notional(symbol: str, notional: float) -> Tuple[float, dict]:
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


def place_order(symbol: str, side: str, amount: float, tif: Optional[str], params: dict):
    ref_price = fetch_last(symbol)
    core = {**(params or {}), "slippage": DEFAULT_SLIPPAGE}
    if tif:
        core["timeInForce"] = tif  # ccxt expects timeInForce
    return ex().create_order(symbol, "market", side, float(amount), ref_price, core)


# -------- Position helpers (for FLIP) --------
def get_open_position(symbol: str) -> dict:
    """
    Returns dict with signed size (>0 long, <0 short), side, entryPrice.
    """
    try:
        positions = ex().fetch_positions([symbol])
    except Exception:
        positions = [p for p in ex().fetch_positions() if p.get("symbol") == symbol]

    for p in positions:
        if p.get("symbol") != symbol:
            continue
        side = p.get("side")
        # size fields vary across ccxt builds/hyperliquid. Try several:
        amt = p.get("positionAmt") or p.get("size") or p.get("contracts") or 0
        try:
            amt = float(amt)
        except Exception:
            amt = 0.0
        signed = amt if side == "long" else (-amt if side == "short" else 0.0)
        return {
            "size": signed,
            "side": side,
            "entryPrice": float(p.get("entryPrice") or 0),
        }
    return {"size": 0.0, "side": None, "entryPrice": 0.0}


def close_position(symbol: str, tif: str = DEFAULT_TIF) -> dict:
    pos = get_open_position(symbol)
    size = float(pos["size"])
    if abs(size) <= 0:
        return {"closed": False, "reason": "flat"}

    step, _, _ = market_meta(symbol)
    amt = floor_to_step(abs(size), step)
    if amt <= 0:
        return {"closed": False, "reason": "zero_after_floor"}

    side = "sell" if size > 0 else "buy"  # sell closes long; buy closes short
    ref_price = fetch_last(symbol)
    params = {"reduceOnly": True, "timeInForce": tif, "slippage": DEFAULT_SLIPPAGE}
    order = ex().create_order(symbol, "market", side, amt, ref_price, params)
    return {"closed": True, "order": order, "sizeClosed": amt, "side": side}


def ensure_leverage(symbol: str, lev: float):
    try:
        if lev:
            ex().set_leverage(lev, symbol)
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)


# -------- Flask app --------
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
    # show what ccxt requires, and whether creds are present
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
    Body (examples):
      {"symbol":"BTC","action":"buy","notional":50,"leverage":20,"tif":"IOC"}
      {"symbol":"SOL","action":"sell","quantity":1}
    Flip behavior:
      - If current position is opposite side, we close it reduce-only, then open the new side.
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        base = (payload.get("symbol") or "").upper().strip()
        if not base:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        hl_symbol = symbol_to_hl(base)

        # Ensure market exists
        ex().market(hl_symbol)

        # Options
        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        leverage = float(payload.get("leverage", DEFAULT_LEVERAGE))

        # Size decision
        qty = payload.get("quantity")
        notional = payload.get("notional")

        debug_info = {}
        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
        else:
            # default to notional if missing (your “one price” flow)
            if notional is None:
                notional = DEFAULT_NOTIONAL
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["amount_from_notional"] = dbg

        if amt <= 0:
            return jsonify({"status": "error", "message": "Computed zero amount", "debug": debug_info}), 400

        # Leverage (best effort)
        ensure_leverage(hl_symbol, leverage)

        # ---- FLIP LOGIC ----
        pos = get_open_position(hl_symbol)   # size > 0 long, < 0 short
        size = float(pos["size"])

        def same_dir(signed, need_side):
            if signed == 0:
                return False
            return (signed > 0 and need_side == "buy") or (signed < 0 and need_side == "sell")

        if size != 0 and not same_dir(size, action):
            closed = close_position(hl_symbol, tif=tif)
            log.info("Flip: closed %s -> %s", hl_symbol, json.dumps(closed, default=str))

        # Open the desired side
        order = place_order(hl_symbol, action, amt, tif, params={})

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "side": action,
            "amount": float(amt),
            "order": order,
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
