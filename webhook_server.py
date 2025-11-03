# webhook_server.py
import os
import math
import json
import logging
from decimal import Decimal
from typing import Optional, Tuple

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ---- ENV / CONFIG ----
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()  # "testnet" or "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()   # 0x...
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip() # 0x... (64 hex)
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "GTC")

# ccxt exchange singleton
_ex = None


def ex() -> ccxt.Exchange:
    global _ex
    if _ex is not None:
        return _ex

    # ccxt hyperliquid requires 'apiKey' (owner wallet) and 'secret' (api wallet private key)
    # We use the API Wallet address as apiKey for signing (per ccxt HL driver),
    # and the PRIVATE_KEY as secret.
    # testnet: enable sandboxMode
    opts = {
        "apiKey": API_WALLET or None,
        "secret": PRIVATE_KEY or None,
        "options": {
            # slippage is still passed per-order, but set a sensible default here too
            "defaultSlippage": DEFAULT_SLIPPAGE,
        },
    }
    hl = ccxt.hyperliquid(opts)
    # Sandbox for testnet
    if NETWORK == "testnet":
        try:
            hl.set_sandbox_mode(True)
            log.info("✅ ccxt Hyperliquid sandbox (testnet) enabled")
        except Exception as e:
            log.warning("Could not enable sandbox (testnet): %s", e)

    # Preload markets for precision/limits
    try:
        hl.load_markets(True)
        log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    except Exception as e:
        log.error("Failed to load markets: %s", e)
        raise

    _ex = hl
    return _ex


def symbol_to_hl(user_symbol: str) -> str:
    # user: "BTC" -> "BTC/USDC:USDC" ; "SOL" -> "SOL/USDC:USDC"
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
    # fallback to orderbook midpoint
    ob = ex().fetch_order_book(symbol, limit=5)
    bid = ob["bids"][0][0] if ob.get("bids") else None
    ask = ob["asks"][0][0] if ob.get("asks") else None
    if bid and ask:
        return float((bid + ask) / 2)
    raise RuntimeError(f"Could not fetch last price for {symbol}")


def market_meta(symbol: str) -> Tuple[float, float, float]:
    """
    Returns (amount_step, min_amount, price_step)
    Falls back sensibly if limits are missing.
    """
    m = ex().market(symbol)
    # ccxt usually provides m['precision']['amount'] as decimal step (e.g., 0.01),
    # but some drivers expose 'amountPrecision' too. Check both.
    amount_step = (
        (m.get("precision") or {}).get("amount")
        or m.get("amountPrecision")
        or 0.00000001
    )
    # min amount (if provided)
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
    Returns (amount, debug)
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
        # bump to one step to avoid zero-size submits
        floored = amount_step

    if floored < min_amount:
        # If even one step is < min_amount, you must use min_amount
        floored = min_amount

    # precision-safe final formatting through ccxt helper
    final_amt = float(ex().amount_to_precision(symbol, floored))
    debug["final_amt"] = final_amt
    return final_amt, debug


def compute_amount_from_notional(symbol: str, notional: float) -> Tuple[float, dict]:
    px = fetch_last(symbol)
    raw_amt = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw_amt)
    dbg.update({"notional": notional, "last_price": px})
    # sanity: confirm notional covers at least min amount
    _, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} "
            f"for {symbol} (min amount {min_amt})."
        )
    return amt, dbg


def place_order(symbol: str, side: str, amount: float, tif: Optional[str], params: dict):
    """
    Market orders: pass a reference price and slippage in params.
    Limit orders: call ex.create_order with price set and postOnly/reduceOnly as desired.
    """
    ref_price = fetch_last(symbol)
    core = {**(params or {}), "slippage": DEFAULT_SLIPPAGE}
    if tif:
        core["tif"] = tif

    # Market order with ref_price for HL (ccxt will compute max slippage price)
    return ex().create_order(symbol, "market", side, float(amount), ref_price, core)


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
    # ccxt.hyperliquid uses apiKey=API_WALLET, secret=PRIVATE_KEY
    try:
        owner_wallet = API_WALLET  # for HL this is the API Wallet addr used to sign
    except Exception:
        owner_wallet = None
    return jsonify({
        "apiWallet_env": API_WALLET,
        "apiWallet_from_privateKey": API_WALLET,
        "network": NETWORK,
        "ownerWallet": owner_wallet
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
        # a lightweight call: fetch balance (if creds are set it works)
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
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.post("/webhook/tradingview")
def tradingview():
    """
    Body:
    {
      "symbol": "SOL",                 # required (base)
      "action": "buy"|"sell",          # required
      "quantity": 1.0,                 # OR
      "notional": 50,                  # use either
      "tif": "IOC"|"GTC",              # optional
      "post_only": true,               # optional (limit path only)
      "reduce_only": true,             # optional
      "price": 180.25                  # optional (limit path)
    }
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

        # Make sure market is known
        try:
            ex().market(hl_symbol)
        except Exception:
            return jsonify({"status": "error", "message": f"Unknown or unsupported market {hl_symbol}"}), 400

        tif = (payload.get("tif") or DEFAULT_TIF).upper()

        qty = payload.get("quantity")
        notional = payload.get("notional")
        price = payload.get("price")  # only for limit orders if you add that path later

        # Decide amount
        debug_info = {}
        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
        elif notional is not None:
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["amount_from_notional"] = dbg
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        # Place market order
        params = {}
        if payload.get("reduce_only") is True:
            params["reduceOnly"] = True

        order = place_order(hl_symbol, action, amt, tif, params)

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "side": action,
            "amount": float(amt),
            "order": order,
            "debug": debug_info
        })

    except ValueError as ve:
        # E.g., notional below minimum
        return jsonify({"status": "error", "message": str(ve)}), 400
    except ccxt.BaseError as ce:
        log.exception("Exchange error")
        return jsonify({"status": "error", "message": f"hyperliquid {str(ce)}"}), 400
    except Exception as e:
        log.exception("Unhandled")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
