# webhook_server.py
import os
import re
import math
import logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ============= ENV / CONFIG =============
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()               # "testnet" or "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()                # 0x... (API wallet address)
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()              # 0x... (private key hex)
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02")) # 2% default slippage
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "IOC").upper()           # IOC|GTC (HL honors IOC/GTC)
APPLY_SET_LEVERAGE = os.getenv("HL_APPLY_SET_LEVERAGE", "false").lower() == "true"

# If "margin": treat {"notional": 50, "leverage": 20} as a ~$1000 position notional.
# If "notional": place ~$50 position regardless of leverage.
NOTIONAL_MODE = os.getenv("HL_NOTIONAL_MODE", "margin").lower()    # "margin" or "notional"

# Accept TV symbols like BTC, BTCUSD, BTCUSDT, SOL, SOLUSDT, etc.
SYMBOL_TV_CLEANER = re.compile(r"[^A-Z]")

_ex: Optional[ccxt.Exchange] = None


def ex() -> ccxt.Exchange:
    global _ex
    if _ex is not None:
        return _ex

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

    # Load markets once
    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))
    _ex = hl
    return _ex


# ============= SYMBOL HELPERS =============
def normalize_base(tv_symbol: str) -> str:
    """
    Map TradingView-ish symbols to base asset.
    Examples:
      BTC -> BTC
      BTCUSD -> BTC
      BTCUSDT -> BTC
      SOLUSDT.P -> SOL (TV variant)
    """
    s = (tv_symbol or "").upper().strip()
    s = SYMBOL_TV_CLEANER.sub("", s)  # keep only A-Z
    # try longest base match that exists on HL
    # Fast path: common suffixes
    for suffix in ("USD", "USDT", "PERP"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s or tv_symbol.upper()


def symbol_to_hl(user_symbol: str) -> str:
    """Convert base to Hyperliquid perp symbol format."""
    base = normalize_base(user_symbol)
    hl_symbol = f"{base}/USDC:USDC"
    return hl_symbol


# ============= MARKET / SIZING HELPERS =============
def fetch_last(symbol: str) -> float:
    """Get last price; fallback to orderbook mid."""
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
    Returns (amount_step, min_amount, price_step).
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
    Floors amount to exchange step and min. Never returns zero if tradable.
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
    # sanity: ensure notional covers at least min amount
    _, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} "
            f"for {symbol} (min amount {min_amt})."
        )
    return amt, dbg


# ============= POSITION & FLIP LOGIC =============
def get_position(symbol: str) -> Dict[str, Any]:
    """
    Return current position info for symbol.
    Output: {"side": "long"|"short"|None, "size": float, "entryPrice": float|None}
    """
    try:
        positions = ex().fetch_positions([symbol])
    except Exception:
        positions = []

    pos = {"side": None, "size": 0.0, "entryPrice": None}

    for p in positions or []:
        sym = p.get("symbol")
        if sym != symbol:
            continue

        # ccxt normalized: p['contracts'] (size in base units), or p['contracts'] may be str
        size = None
        for key in ("contracts", "contractSize", "size", "amount"):
            if key in p and p[key] not in (None, ""):
                try:
                    size = float(p[key])
                    break
                except Exception:
                    pass

        if not size:
            # try absolute of 'info' fields
            info = p.get("info") or {}
            for key in ("sz", "totalSz", "positionSize"):
                if key in info:
                    try:
                        size = abs(float(info[key]))
                        break
                    except Exception:
                        pass

        # Side
        side = p.get("side")
        if not side:
            # infer from signed size if available
            signed = None
            for k in ("contracts", "size", "amount"):
                if k in p and p[k] not in (None, ""):
                    try:
                        signed = float(p[k])
                        break
                    except Exception:
                        pass
            if signed is not None:
                if signed > 0:
                    side = "long"
                elif signed < 0:
                    side = "short"

        ep = None
        for k in ("entryPrice", "avgPrice", "average"):
            if k in p and p[k] not in (None, ""):
                try:
                    ep = float(p[k])
                    break
                except Exception:
                    pass

        if size and size > 0:
            pos["size"] = size
            pos["side"] = side
            pos["entryPrice"] = ep
            break

    return pos


def reduce_only_close(symbol: str, side_to_close: Optional[str], size: float, tif: str) -> Optional[dict]:
    """
    Submit a reduceOnly market order that offsets the current position entirely.
    side_to_close: "long" -> send 'sell'; "short" -> send 'buy'
    """
    if not side_to_close or size <= 0:
        return None

    opposite_side = "sell" if side_to_close == "long" else "buy"
    ref_price = fetch_last(symbol)
    params = {"reduceOnly": True, "tif": tif, "slippage": DEFAULT_SLIPPAGE}
    log.info("Closing %s %s size=%.6f", symbol, side_to_close, size)
    return ex().create_order(symbol, "market", opposite_side, float(size), ref_price, params)


def place_market(symbol: str, side: str, amount: float, tif: str, extra_params: Optional[dict] = None):
    ref_price = fetch_last(symbol)
    core = {"slippage": DEFAULT_SLIPPAGE, "tif": tif}
    if extra_params:
        core.update(extra_params)
    return ex().create_order(symbol, "market", side, float(amount), ref_price, core)


def ensure_leverage(symbol: str, leverage: Optional[float]):
    """Optional: CCXT HL usually doesn't set leverage via API; skip or warn."""
    if not APPLY_SET_LEVERAGE or not leverage:
        return
    try:
        ex().set_leverage(float(leverage), symbol)
    except Exception as e:
        log.warning("set_leverage failed for %s: %s (continuing)", symbol, e)


# ============= FLASK APP / ROUTES =============
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
    try:
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
    Body (examples):
      {"symbol":"BTC","action":"buy","notional":50,"leverage":20,"tif":"IOC"}
      {"symbol":"SOLUSDT","action":"sell","quantity":1,"tif":"IOC"}
    Behavior:
      - Always maintain exactly one position per symbol.
      - If signal side != current side: close existing with reduceOnly, then open new side.
      - If same side: optionally add (we currently just ensure side is maintained; you can choose to add).
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        user_sym = (payload.get("symbol") or "").strip()
        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        leverage = payload.get("leverage")
        qty = payload.get("quantity")
        notional = payload.get("notional")

        # Resolve HL symbol
        hl_symbol = symbol_to_hl(user_sym)
        log.info("Resolved symbol '%s' -> '%s'", user_sym, hl_symbol)

        # Verify market exists
        try:
            ex().market(hl_symbol)
        except Exception:
            return jsonify({"status": "error", "message": f"Unknown or unsupported market {hl_symbol}"}), 400

        # Ensure leverage (optional / usually noop on HL)
        ensure_leverage(hl_symbol, leverage)

        # --- Determine desired side & size ---
        # Compute desired amount (base units). If qty provided, clamp; else compute from notional.
        debug_info = {"symbol": hl_symbol, "mode": NOTIONAL_MODE}
        if qty is not None:
            desired_amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
            effective_notional = None
        elif notional is not None:
            eff = float(notional)
            if NOTIONAL_MODE == "margin" and leverage:
                eff *= float(leverage)
            desired_amt, dbg = compute_amount_from_notional(hl_symbol, eff)
            debug_info["amount_from_notional"] = dbg
            effective_notional = eff
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        desired_side = "long" if action == "buy" else "short"

        # --- Check current position and flip if needed ---
        pos = get_position(hl_symbol)  # {"side": None|"long"|"short", "size": float}
        debug_info["pre_position"] = pos

        close_resp = None
        open_resp = None
        added_resp = None

        # 1) If a position exists and it's the opposite side, close it completely.
        if pos["side"] and pos["side"] != desired_side and pos["size"] > 0:
            close_resp = reduce_only_close(hl_symbol, pos["side"], pos["size"], tif)

        # 2) If no position or we just closed opposite, open desired side with desired_amt.
        if (not pos["side"]) or (pos["side"] != desired_side):
            side_word = "buy" if desired_side == "long" else "sell"
            open_resp = place_market(hl_symbol, side_word, desired_amt, tif)
        else:
            # Already same side; you can choose to "add" or skip.
            # Here we simply **add** to position by desired_amt (comment out if not desired).
            side_word = "buy" if desired_side == "long" else "sell"
            added_resp = place_market(hl_symbol, side_word, desired_amt, tif)

        out = {
            "status": "ok",
            "symbol": hl_symbol,
            "desired_side": desired_side,
            "desired_amount": float(desired_amt),
            "tif": tif,
            "notional_mode": NOTIONAL_MODE,
            "effective_notional_used": effective_notional,
            "actions": {
                "closed_opposite": close_resp,
                "opened_desired": open_resp,
                "added_same_side": added_resp,
            },
            "debug": debug_info,
        }
        return jsonify(out)

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
