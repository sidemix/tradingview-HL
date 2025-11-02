# webhook_server.py
import os
import re
import math
import json
import logging
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify
import ccxt

# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

# -------------------------------
# ENV / CONFIG
# -------------------------------
NETWORK = os.getenv("HL_NETWORK", "testnet").lower()     # "testnet" | "mainnet"
API_WALLET = os.getenv("HL_API_WALLET", "").strip()      # 0x... (address)
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip()    # 0x... (64 hex)
DEFAULT_SLIPPAGE = float(os.getenv("HL_DEFAULT_SLIPPAGE", "0.02"))  # 2%
DEFAULT_TIF = os.getenv("HL_DEFAULT_TIF", "GTC").upper()
DEFAULT_LEVERAGE = float(os.getenv("HL_DEFAULT_LEVERAGE", "20"))     # used when alert omits leverage

# -------------------------------
# ccxt exchange singleton
# -------------------------------
_ex = None


def ex() -> ccxt.Exchange:
    """Return (and memoize) a configured ccxt.hyperliquid instance."""
    global _ex
    if _ex is not None:
        return _ex

    opts = {
        # ccxt.hyperliquid reads these for ECDSA signing:
        "apiKey": API_WALLET or None,         # some ccxt versions use this as walletAddress
        "walletAddress": API_WALLET or None,  # others read this field
        "privateKey": PRIVATE_KEY or None,
        "options": {
            "defaultSlippage": DEFAULT_SLIPPAGE,  # used if we don't pass "slippage"
        },
    }
    hl = ccxt.hyperliquid(opts)

    if NETWORK == "testnet":
        try:
            hl.set_sandbox_mode(True)
            log.info("✅ ccxt Hyperliquid sandbox (testnet) enabled")
        except Exception as e:
            log.warning("Could not enable sandbox mode: %s", e)

    # load markets up front (also primes precision/limits)
    hl.load_markets(True)
    log.info("✅ Markets loaded: %s symbols", len(hl.markets))

    _ex = hl
    return _ex


# -------------------------------
# Symbol handling
# -------------------------------

# Accept common forms: BTC, BTCUSD, BTCUSDT, ETHUSD.P, etc.
_STABLE_SUFFIX = re.compile(r"(USD|USDT|USDC|USD[Pp]?|USDT[Pp]?|USDC[Pp]?)$")


def normalize_base(user_symbol: str) -> str:
    """
    Turn many user/TV variants into an HL base (e.g., 'BTCUSD' → 'BTC').
    """
    s = (user_symbol or "").upper().strip()
    s = s.replace("PERP", "")
    s = s.replace("SPOT", "")
    s = s.replace(":", "").replace("/", "")
    s = s.replace(".P", "").replace("T.P", "")  # absorb a few exchange quirks

    # if looks like 'BTCUSD' or 'ETHUSDT' → strip trailing stable suffix
    m = _STABLE_SUFFIX.search(s)
    if m and len(s) > m.start():
        return s[: m.start()]

    return s


def to_hl_symbol(user_symbol: str) -> str:
    """
    Convert any input into HL unified symbol 'BASE/USDC:USDC'.
    """
    base = normalize_base(user_symbol)
    return f"{base}/USDC:USDC"


# -------------------------------
# Market helpers
# -------------------------------
def fetch_last(symbol: str) -> float:
    """
    Get a usable last price; fallback to midpoint if ticker.last is missing.
    """
    try:
        t = ex().fetch_ticker(symbol)
        px = t.get("last") or t.get("close")
        if px:
            return float(px)
    except Exception:
        pass

    book = ex().fetch_order_book(symbol, limit=5)
    bid = book["bids"][0][0] if book.get("bids") else None
    ask = book["asks"][0][0] if book.get("asks") else None
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
    Floors amount to step, clamps to min size, never returns zero if feasibly tradable.
    """
    amount_step, min_amount, _ = market_meta(symbol)
    floored = floor_to_step(float(raw_amount), amount_step)
    dbg = {
        "raw_amount": float(raw_amount),
        "amount_step": amount_step,
        "min_amount": min_amount,
        "floored": floored,
    }

    if floored <= 0:
        floored = amount_step
    if floored < min_amount:
        floored = min_amount

    final_amt = float(ex().amount_to_precision(symbol, floored))
    dbg["final_amt"] = final_amt
    return final_amt, dbg


def compute_amount_from_notional(symbol: str, notional: float) -> Tuple[float, Dict[str, Any]]:
    """
    Convert a USD notional into base amount, respecting precision/mins.
    """
    px = fetch_last(symbol)
    raw = float(notional) / float(px)
    amt, dbg = clamp_amount(symbol, raw)
    dbg.update({"notional": float(notional), "last_price": float(px)})
    # sanity: ensure notional >= min cost
    _, min_amt, _ = market_meta(symbol)
    min_cost = min_amt * px
    if amt <= 0 or notional < min_cost:
        raise ValueError(
            f"Notional ${notional:.2f} below minimum ~${min_cost:.2f} for {symbol} (min amount {min_amt})."
        )
    return amt, dbg


# -------------------------------
# Positions / Flip logic
# -------------------------------
def get_position_size(symbol: str) -> float:
    """
    Returns signed base size:
      > 0  long, < 0 short, 0 flat
    """
    try:
        # ccxt unified endpoint; may be slow on testnet, but fine for webhook use
        positions = ex().fetch_positions([symbol])
        for p in positions:
            if p.get("symbol") == symbol:
                # ccxt unified: contracts/amount can differ by exchange; use 'contracts' or 'amount'
                size = p.get("contracts")
                if size is None:
                    size = p.get("amount")
                if size is None:
                    # fallback from info if available
                    info = p.get("info") or {}
                    size = float(info.get("szi", 0)) if "szi" in info else 0.0
                try:
                    return float(size or 0)
                except Exception:
                    return 0.0
    except Exception:
        pass
    return 0.0


def close_if_opposite(symbol: str, incoming_side: str, tif: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    If we hold an opposite position, submit a reduceOnly market to flat.
    Returns the close order dict if sent, else None.
    """
    pos = get_position_size(symbol)
    if pos == 0:
        return None

    if incoming_side == "buy" and pos < 0:
        size_to_close = abs(pos)
        return place_market(symbol, "buy", size_to_close, tif, reduce_only=True)

    if incoming_side == "sell" and pos > 0:
        size_to_close = abs(pos)
        return place_market(symbol, "sell", size_to_close, tif, reduce_only=True)

    return None


# -------------------------------
# Order placement
# -------------------------------
def place_market(symbol: str, side: str, amount: float, tif: Optional[str], reduce_only: bool = False) -> Dict[str, Any]:
    """
    Place a market order by passing a reference price and slippage (HL requirement).
    """
    ref_price = fetch_last(symbol)
    params = {"slippage": DEFAULT_SLIPPAGE}
    if tif:
        params["tif"] = tif
    if reduce_only:
        params["reduceOnly"] = True

    return ex().create_order(symbol, "market", side, float(amount), ref_price, params)


# -------------------------------
# Flask App
# -------------------------------
app = Flask(__name__)


@app.get("/")
def root():
    return jsonify({
        "status": "ok",
        "network": NETWORK,
        "whoami": "/whoami",
        "health": "/health",
        "markets": "/markets?base=SOL (or &symbol=SOL/USDC:USDC)",
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
        "defaults": {
            "slippage": DEFAULT_SLIPPAGE,
            "tif": DEFAULT_TIF,
            "leverage": DEFAULT_LEVERAGE,
        }
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
    TradingView alert JSON examples:

    Use fixed collateral with leverage:
    {
      "symbol": "SOL" | "SOLUSDT" | "BTCUSD" | "DOGE",
      "action": "buy" | "sell",
      "notional": 50,            # collateral dollars
      "leverage": 10,            # optional; falls back to HL_DEFAULT_LEVERAGE
      "tif": "IOC"               # optional; default HL_DEFAULT_TIF
    }

    Or use base units directly:
    {
      "symbol": "BTC",
      "action": "sell",
      "quantity": 0.02,
      "tif": "IOC"
    }
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        log.info("Received alert: %s", payload)

        # --- Symbol handling ---
        incoming_sym = (payload.get("symbol") or "").strip()
        if not incoming_sym:
            return jsonify({"status": "error", "message": "Missing symbol"}), 400

        hl_symbol = to_hl_symbol(incoming_sym)
        log.info("Resolved symbol '%s' -> '%s'", incoming_sym, hl_symbol)

        # Ensure market exists
        try:
            ex().market(hl_symbol)
        except Exception:
            return jsonify({"status": "error", "message": f"Unknown or unsupported market {hl_symbol}"}), 400

        # --- Side / TIF ---
        side = (payload.get("action") or "").lower().strip()
        if side not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400
        tif = (payload.get("tif") or DEFAULT_TIF).upper()

        # --- Sizing ---
        qty = payload.get("quantity")
        notional = payload.get("notional")
        leverage = float(payload.get("leverage") or DEFAULT_LEVERAGE)

        debug_info: Dict[str, Any] = {"leverage": leverage}

        if qty is not None:
            amount, dbg = clamp_amount(hl_symbol, float(qty))
            debug_info["amount_from_quantity"] = dbg
            effective_notional = None
        elif notional is not None:
            # interpret notional as collateral; multiply by leverage to get position size in USD
            effective_notional = float(notional) * leverage
            amount, dbg = compute_amount_from_notional(hl_symbol, effective_notional)
            debug_info["amount_from_notional"] = dbg
            debug_info["effective_notional"] = effective_notional
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional"}), 400

        # --- Flip logic: always in a position ---
        close_order = close_if_opposite(hl_symbol, side, tif)

        # --- Open the new side ---
        open_order = place_market(hl_symbol, side, amount, tif, reduce_only=False)

        resp = {
            "status": "ok",
            "symbol": hl_symbol,
            "side": side,
            "amount": float(amount),
            "close_order": close_order,
            "open_order": open_order,
            "debug": debug_info
        }
        return jsonify(resp)

    except ValueError as ve:
        # e.g. min notional failures
        return jsonify({"status": "error", "message": str(ve)}), 400
    except ccxt.BaseError as ce:
        # Surface common HL messages nicely
        msg = str(ce)
        friendly = None
        if "open interest is at cap" in msg.lower():
            friendly = "Rejected: Open interest cap on this asset (HL testnet often caps popular pairs)."
        elif "price too far from oracle" in msg.lower():
            friendly = "Rejected: Price too far from oracle (try lower slippage or retry)."
        elif "reduce only" in msg.lower() and "increasing" in msg.lower():
            friendly = "Reduce-only prevented increase; check flip logic / position state."

        log.exception("Exchange error")
        return jsonify({"status": "error", "message": f"hyperliquid {friendly or msg}"}), 400
    except Exception as e:
        log.exception("Unhandled")
        return jsonify({"status": "error", "message": str(e)}), 500


# -------------------------------
# Entrypoint
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
