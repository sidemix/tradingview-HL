import os
import math
from decimal import Decimal
from flask import Flask, request, jsonify

import ccxt

#
# --- Config / Env ---
#
OWNER_ADDR      = os.environ.get("HL_OWNER", "").strip()
PRIVATE_KEY_HEX = os.environ.get("HL_PRIVATE_KEY", "").strip()
API_WALLET      = os.environ.get("HL_API_WALLET", "").strip()

DEFAULT_SLIPPAGE = 0.05  # 5% max slippage reference for market orders
DEFAULT_TIF      = "Ioc" # Hyperliquid expects 'Gtc' | 'Ioc' | 'Alo'

app = Flask(__name__)

def _assert_env():
    missing = []
    if not OWNER_ADDR:      missing.append("HL_OWNER")
    if not PRIVATE_KEY_HEX: missing.append("HL_PRIVATE_KEY")
    if not API_WALLET:      missing.append("HL_API_WALLET")
    if missing:
        raise RuntimeError(f"Missing env: {', '.join(missing)}")

def make_ex():
    """
    Build a CCXT Hyperliquid exchange instance for TESTNET and ensure we use
    your owner wallet + private key. We also keep options minimal and pin the vault per order.
    """
    _assert_env()

    ex = ccxt.hyperliquid({
        # CCXT expects these exact keys for Hyperliquid:
        "walletAddress": OWNER_ADDR,
        "privateKey": PRIVATE_KEY_HEX,
        "options": {
            # You can also set default slippage here (string or float accepted).
            "slippage": DEFAULT_SLIPPAGE,
        },
        "enableRateLimit": True,
    })
    # Testnet routing
    ex.set_sandbox_mode(True)
    return ex

def normalize_symbol(sym: str) -> str:
    """
    Map incoming symbols like 'BTC' or 'BTC/USDT' to Hyperliquid unified symbol.
    HL uses USDC as quote for perps in CCXT: 'BTC/USDC', 'SOL/USDC', etc.
    """
    s = sym.upper().replace("USDT", "USDC").replace("/USD", "/USDC").replace("/USDC", "")
    return f"{s}/USDC"

def fetch_last(ex, symbol: str) -> float:
    """
    HL's CCXT requires a reference price for market orders. Use ticker last/mark if present.
    """
    # fetch_ticker throws if market unknown -> let caller handle
    t = ex.fetch_ticker(symbol)
    px = t.get("last") or t.get("mark") or t.get("ask") or t.get("bid")
    if px is None:
        raise RuntimeError(f"Could not fetch last price for {symbol}")
    return float(px)

def to_float(x):
    if isinstance(x, Decimal):
        return float(x)
    return float(x)

def compute_amount_from_notional(ex, symbol: str, notional: float) -> float:
    last = fetch_last(ex, symbol)
    if last <= 0:
        raise RuntimeError(f"Bad last price for {symbol}: {last}")
    amt = notional / last
    # Respect amount precision
    market = ex.market(symbol)
    precision = market.get("precision", {}).get("amount", 6)
    return float(ex.amount_to_precision(symbol, round(amt, precision)))

@app.get("/whoami")
def whoami():
    return jsonify({
        "network": "testnet",
        "ownerWallet": OWNER_ADDR,
        "apiWallet_env": API_WALLET,
        "apiWallet_from_privateKey": OWNER_ADDR,  # CCXT signs with HL_PRIVATE_KEY that controls OWNER_ADDR
    })

@app.get("/health")
def health():
    try:
        ex = make_ex()
        bal = None
        try:
            # swap balance by default
            bal = ex.fetch_balance({"type": "swap"})
            equity = None
            # HL returns { info: {...}, total: {...}, free: {...} }
            # try to surface something friendly
            if "info" in bal and isinstance(bal["info"], dict):
                equity = bal["info"].get("equity")
        except Exception:
            equity = None

        return jsonify({
            "status": "healthy",
            "network": "testnet",
            "trading": "active",
            "credentials_set": bool(OWNER_ADDR and PRIVATE_KEY_HEX and API_WALLET),
            "balance": equity,
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "credentials_set": False,
        }), 500

@app.post("/webhook/tradingview")
def tradingview():
    """
    Body examples:
    - {"symbol":"BTC","action":"buy","quantity":0.05}
    - {"symbol":"SOL","action":"buy","notional":50,"tif":"IOC"}
    - {"symbol":"ETH","action":"sell","quantity":1.25,"post_only":true,"tif":"GTC"}
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    try:
        ex = make_ex()
        sym_in = (payload.get("symbol") or "").strip()
        if not sym_in:
            return jsonify({"status": "error", "message": "symbol is required"}), 400

        hl_symbol = normalize_symbol(sym_in)

        action = (payload.get("action") or "").lower().strip()
        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be 'buy' or 'sell'"}), 400
        side = "buy" if action == "buy" else "sell"

        # time in force & flags
        tif_in = payload.get("tif") or payload.get("time_in_force") or DEFAULT_TIF
        tif_map = {"GTC": "Gtc", "IOC": "Ioc", "ALO": "Alo"}
        time_in_force = tif_map.get(str(tif_in).upper(), DEFAULT_TIF)

        post_only   = bool(payload.get("post_only", False))
        reduce_only = bool(payload.get("reduce_only", False))

        # amount: quantity (base) OR notional (quote)
        qty = payload.get("quantity")
        notion = payload.get("notional")

        # Always fetch a reference price for market orders (HL requires it)
        ref_price = fetch_last(ex, hl_symbol)

        if qty is not None:
            amount = to_float(qty)
        elif notion is not None:
            amount = compute_amount_from_notional(ex, hl_symbol, to_float(notion))
        else:
            return jsonify({"status": "error", "message": "Provide 'quantity' or 'notional'"}), 400

        # Optional explicit price for limit orders
        limit_px = payload.get("price")
        limit_px = to_float(limit_px) if limit_px is not None else None

        # Build params; CRITICAL: pin your API wallet as the vaultAddress on *every* order
        params = {
            "vaultAddress": API_WALLET,
            "timeInForce": time_in_force,
            "postOnly": post_only,
            "reduceOnly": reduce_only,
            # For market orders, CCXT Hyperliquid uses 'slippage' & needs a reference price
            "slippage": DEFAULT_SLIPPAGE,
        }

        # Choose order type: limit if price provided; else market (with ref price)
        if limit_px is not None:
            order = ex.create_order(hl_symbol, "limit", side, float(amount), float(limit_px), params)
        else:
            # CCXT Hyperliquid allows passing price for market request to compute slippage bounds
            order = ex.create_order(hl_symbol, "market", side, float(amount), ref_price, params)

        return jsonify({"status": "ok", "message": "order placed", "result": order})

    except ccxt.BaseError as e:
        # Surface CCXT exchange errors (including HL “wallet does not exist”)
        return jsonify({"status": "error", "message": f"hyperliquid {str(e)}"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    # Local run: FLASK needs host/port
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
