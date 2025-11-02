# webhook_server.py
import os
import math
import logging
from typing import Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# -------- ENV / CONFIG --------
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()  # "testnet" or "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()   # 0x...
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip() # 0x... (64 hex)

DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "IOC").upper()
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_LEVERAGE = int(os.getenv("HL_DEFAULT_LEVERAGE", "10"))

_ex = None  # ccxt singleton


def ex() -> ccxt.Exchange:
    global _ex
    if _ex is not None:
        return _ex

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


# -------- HELPERS --------
def normalize_user_symbol(s: str) -> str:
    """
    Accepts: 'BTC', 'BTCUSD', 'BTCUSDT', 'btc', etc.
    Returns Hyperliquid symbol: 'BTC/USDC:USDC'
    """
    base = (s or "").upper().strip()
    for suff in ("USDT", "USD", "USDC", "PERP"):
        if base.endswith(suff) and len(base) > len(suff):
            base = base[: -len(suff)]
            break
    base = base.strip()
    return f"{base}/USDC:USDC"


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


def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, Dict[str, Any]]:
    step, min_amt, _ = market_meta(symbol)
    floored = floor_to_step(max(raw_amount, 0.0), step)
    if floored <= 0:
        floored = step
    if floored < min_amt:
        floored = min_amt
    final_amt = float(ex().amount_to_precision(symbol, floored))
    return final_amt, {
        "raw_amount": raw_amount,
        "amount_step": step,
        "min_amount": min_amt,
        "floored": floored,
        "final_amt": final_amt,
    }


def fetch_last(symbol: str) -> float:
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


def amount_from_notional(symbol: str, notional: float) -> Tuple[float, Dict[str, Any]]:
    px = fetch_last(symbol)
    raw = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw)
    step, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} "
            f"for {symbol} (min amount {min_amt}, step {step})."
        )
    dbg.update({"notional": notional, "last_price": px})
    return amt, dbg


def set_leverage(symbol: str, lev: int):
    try:
        if hasattr(ex(), "set_leverage"):
            ex().set_leverage(lev, symbol)
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)


def place_market(symbol: str, side: str, amount: float, tif: str, extra: Dict[str, Any] = None):
    ref = fetch_last(symbol)
    params = {"slippage": DEFAULT_SLIPPAGE}
    if tif:
        params["tif"] = tif
    if extra:
        params.update(extra)
    return ex().create_order(symbol, "market", side, float(amount), ref, params)


# -------- FLASK --------
app = Flask(__name__)


@app.get("/")
def root():
    return jsonify({
        "status": "ok",
        "network": NETWORK,
        "whoami": "/whoami",
        "health": "/health",
        "markets": "/markets?symbol=SOL/USDC:USDC or ?base=SOL",
        "webhook": "/webhook/tradingview"
    })


@app.get("/whoami")
def whoami():
    return jsonify({
        "network": NETWORK,
        "apiWallet_env": API_WALLET,
        "ownerWallet": API_WALLET,
        "privateKey_present": bool(PRIVATE_KEY),
        "ccxt_required": getattr(ex(), "requiredCredentials", None),
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
    Minimal execution:
    - normalize symbol
    - compute amount from notional OR use quantity
    - optional leverage set
    - place ONE market order
    Example body:
      {"symbol":"BTCUSD","action":"buy","notional":50,"leverage":20,"tif":"IOC"}
      {"symbol":"SOL","action":"sell","quantity":1.25}
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        sym_raw = (payload.get("symbol") or "").strip()
        if not sym_raw:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        hl_symbol = normalize_user_symbol(sym_raw)
        log.info("Resolved symbol '%s' -> '%s'", sym_raw, hl_symbol)
        ex().market(hl_symbol)  # ensure valid

        action = (payload.get("action") or "").lower()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        leverage = int(payload.get("leverage") or DEFAULT_LEVERAGE)
        set_leverage(hl_symbol, leverage)

        qty = payload.get("quantity")
        notional = payload.get("notional")
        amount_debug = {}

        if qty is not None:
            amount, dbg = clamp_amount(hl_symbol, float(qty))
            amount_debug["from_quantity"] = dbg
        elif notional is not None:
            amount, dbg = amount_from_notional(hl_symbol, float(notional))
            amount_debug["from_notional"] = dbg
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        # Single market order; HL will flip if needed
        order = place_market(hl_symbol, action, amount, tif)

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "side": action,
            "amount": amount,
            "leverage": leverage,
            "tif": tif,
            "order": order,
            "amount_debug": amount_debug
        })

    except ccxt.BaseError as ce:
        log.exception("Exchange error")
        return jsonify({"status": "error", "message": f"hyperliquid {str(ce)}"}), 400
    except Exception as e:
        log.exception("Unhandled")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
