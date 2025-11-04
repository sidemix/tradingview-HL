# webhook_server.py
import os
import math
import logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# ── ENV / CONFIG ─────────────────────────────────────────────────────────────────

# "testnet" or "mainnet"
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()

# Hyperliquid API wallet (EOA) address and its private key for signing
API_WALLET   = (os.getenv("HL_API_WALLET") or "").strip()
PRIVATE_KEY  = (os.getenv("HL_PRIVATE_KEY") or "").strip()

# Default behavior
DEFAULT_TIF       = os.getenv("HL_DEFAULT_TIF", "IOC").upper()           # IOC/GTC
DEFAULT_SLIPPAGE  = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))      # 2%

# Optional allow-list of bases you actually want to trade (post-normalization).
# Leave empty to allow anything HL lists.
ALLOWED_SYMBOLS = {
    # Example allow-list (bases only, left→right as requested previously)
    # "BTC","ETH","SOL","LINK","BNB","AVAX","DOGE","TAO","TON","UNI",
    # "NEAR","SUI","PAXG","STBL","HYPE","ZORA","ETHFI","MNT","CRV","AIXBT"
}

# ── ccxt exchange singleton ──────────────────────────────────────────────────────
_ex = None

def ex() -> ccxt.Exchange:
    global _ex
    if _ex is not None:
        return _ex

    opts = {
        # ccxt.hyperliquid looks for these for signing:
        "apiKey": API_WALLET or None,          # wallet address
        "walletAddress": API_WALLET or None,   # some versions read this
        "privateKey": PRIVATE_KEY or None,     # 0x… hex private key
        "options": {
            "defaultSlippage": DEFAULT_SLIPPAGE,  # market order tolerance
        },
    }
    hl = ccxt.hyperliquid(opts)

    if NETWORK == "testnet":
        try:
            hl.set_sandbox_mode(True)
            log.info("✅ ccxt Hyperliquid sandbox (testnet) enabled")
        except Exception as e:
            log.warning("Could not enable sandbox: %s", e)

    # Preload markets once for precision/limits
    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))

    _ex = hl
    return _ex

# ── TradingView symbol → Hyperliquid base normalization ──────────────────────────

EXCEPT_BASE_MAP = {
    # TradingView symbol  -> HL base (one-offs / renames)
    "XPLUSDT": "XPL", "XPLUSDT.P": "XPL",
    "VIRTUALUSDT": "VIRTUAL", "VIRTUALUSDT.P": "VIRTUAL",
    "OGUSDT": "OG", "OGUSDT.P": "OG",
    # Add more exceptional mappings here if you encounter them
}

def _tv_to_base(sym: str) -> str:
    """
    Convert a TradingView ticker (e.g., BINANCE:BTCUSDT.P) into HL 'base' (e.g., BTC).
    Handles suffixes like '.P', and common quote tails ('USD','USDT').
    Applies an exception map for odd cases (XPL, OG, etc.).
    """
    s = (sym or "").upper().strip()
    if ":" in s:
        s = s.split(":")[-1]  # drop exchange prefix if present

    if s in EXCEPT_BASE_MAP:
        return EXCEPT_BASE_MAP[s]

    # strip paper/perp suffix '.P' used by some feeds
    if s.endswith(".P"):
        s = s[:-2]

    # strip common quote suffixes
    for tail in ("USDT", "USD"):
        if s.endswith(tail):
            s = s[: -len(tail)]
            break

    # If it's already just a base (BTC/ETH/SOL/etc), it falls through unchanged
    return s

def symbol_to_hl(user_symbol: str) -> str:
    """
    Map user/TV symbol to ccxt Hyperliquid market symbol.
    e.g. 'BTCUSD'/'BTCUSDT'/'BTCUSDT.P'/'BTC' -> 'BTC/USDC:USDC'
         'TRUMPUSDT.P' -> 'TRUMP/USDC:USDC'
         'XPLUSDT.P'   -> 'XPL/USDC:USDC'
    """
    base = _tv_to_base(user_symbol)
    return f"{base}/USDC:USDC"

# ── Market helpers (amount steps / min sizes / prices) ───────────────────────────

def fetch_last(symbol: str) -> float:
    """Get a usable last/close; fallback to mid from order book."""
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
    """Return (amount_step, min_amount, price_step) with sensible fallbacks."""
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

def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step

def clamp_amount(symbol: str, raw_amount: float) -> Tuple[float, Dict[str, Any]]:
    """
    Floors to symbol amount step, enforces min size, never returns 0 if trade is feasible.
    """
    amount_step, min_amount, _ = market_meta(symbol)
    floored = _floor_to_step(raw_amount, amount_step)
    if floored <= 0:
        floored = amount_step
    if floored < min_amount:
        floored = min_amount
    final_amt = float(ex().amount_to_precision(symbol, floored))
    return final_amt, {
        "raw_amount": raw_amount,
        "amount_step": amount_step,
        "min_amount": min_amount,
        "floored": floored,
        "final_amt": final_amt,
    }

def compute_amount_from_notional(symbol: str, notional: float) -> Tuple[float, Dict[str, Any]]:
    px = fetch_last(symbol)
    raw = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw)
    dbg.update({"notional": notional, "last_price": px})
    # sanity check against min notional
    _, min_amt, _ = market_meta(symbol)
    min_notional = min_amt * px
    if amt <= 0 or notional < min_notional:
        raise ValueError(
            f"Notional ${notional:.2f} is below minimum ~${min_notional:.2f} for {symbol} (min amount {min_amt})."
        )
    return amt, dbg

# ── Order placement: simple "fire-and-let-HL-flip" ──────────────────────────────

def place_market(symbol: str, side: str, amount: float, tif: Optional[str] = None):
    """
    Submit a MARKET order and let Hyperliquid handle flips (auto-close + reverse).
    We pass a reference price + slippage so ccxt/HL computes bounds.
    """
    ref = fetch_last(symbol)
    params = {"slippage": DEFAULT_SLIPPAGE}
    if tif:
        params["tif"] = tif
    # No manual close/reopen logic — HL flips automatically if side changes.
    return ex().create_order(symbol, "market", side, float(amount), ref, params)

# ── Flask app ───────────────────────────────────────────────────────────────────

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

@app.post("/webhook/tradingview")
def tradingview():
    """
    Body (send from TradingView alert message):
    {
      "symbol":   "BTCUSD" | "BTCUSDT" | "BTCUSDT.P" | "BTC" | "BINANCE:BTCUSDT.P",  # required
      "action":   "buy" | "sell",                                                       # required
      "quantity": 0.25,                                                                 # OR
      "notional": 50,                                                                   # use one
      "tif":      "IOC" | "GTC"                                                         # optional (defaults to IOC)
    }
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        raw_symbol = (payload.get("symbol") or "").strip()
        if not raw_symbol:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400

        hl_symbol = symbol_to_hl(raw_symbol)
        base = hl_symbol.split("/")[0]
        log.info("Resolved symbol '%s' -> '%s'", raw_symbol, hl_symbol)

        # Optional allow-list check (skip if list is empty)
        if ALLOWED_SYMBOLS and base not in ALLOWED_SYMBOLS:
            return jsonify({"status": "error",
                            "message": f"Base '{base}' not in ALLOWED_SYMBOLS"}), 400

        # Ensure the market exists
        ex().market(hl_symbol)

        tif = (payload.get("tif") or DEFAULT_TIF).upper()
        qty = payload.get("quantity")
        notional = payload.get("notional")

        debug_info = {}
        if qty is not None:
            amt, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["from_quantity"] = dbg
        elif notional is not None:
            amt, dbg = compute_amount_from_notional(hl_symbol, float(notional))
            debug_info["from_notional"] = dbg
        else:
            return jsonify({"status": "error",
                            "message": "Provide either 'quantity' or 'notional'"}), 400

        order = place_market(hl_symbol, action, amt, tif)

        return jsonify({
            "status": "ok",
            "symbol": hl_symbol,
            "side": action,
            "tif": tif,
            "amount": float(amt),
            "amount_debug": debug_info,
            "order": order
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
