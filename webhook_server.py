# webhook_server.py
import os
import math
import time
import traceback
from typing import Optional, Tuple, Dict, Any

from flask import Flask, request, jsonify

import ccxt

app = Flask(__name__)

# ---------- ENV ----------
HL_NETWORK = os.getenv("HL_NETWORK", "testnet").lower()  # "testnet" | "mainnet"
HL_API_WALLET = os.getenv("HL_API_WALLET", "").strip()   # 0x...
HL_PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "").strip() # 0x...

# Default order params
DEFAULT_TIF = "GTC"      # "GTC" | "IOC" | "ALO" etc. (CCXT passes thru)
DEFAULT_SLIPPAGE = 0.02  # 2% max slippage tolerance for market orders
DEFAULT_LEVERAGE = 5     # You can adjust per symbol later if needed

# ---------- EXCHANGE BOOT ----------
def make_exchange() -> ccxt.Exchange:
    """
    Create a CCXT Hyperliquid instance correctly configured for testnet/mainnet.
    CCXT expects 'walletAddress' + 'privateKey' for Hyperliquid.
    """
    opts = {
        "walletAddress": HL_API_WALLET,
        "privateKey": HL_PRIVATE_KEY,
        "options": {
            # CCXT flag so it will use testnet API host & signing domain
            "testnet": HL_NETWORK == "testnet",
        },
    }
    ex = ccxt.hyperliquid(opts)
    # Load markets up-front so symbol lookup works
    ex.load_markets()
    return ex

def validate_credentials(ex: ccxt.Exchange) -> Tuple[bool, str]:
    if not HL_API_WALLET or not HL_PRIVATE_KEY or not HL_API_WALLET.startswith("0x") or not HL_PRIVATE_KEY.startswith("0x"):
        return False, "Missing or malformed HL_API_WALLET / HL_PRIVATE_KEY env"
    # CCXT exposes requiredCredentials list — make sure it includes both
    req = getattr(ex, "requiredCredentials", {})
    if not req.get("privateKey", False):
        return False, "CCXT hyperliquid: privateKey is not marked required (unexpected version?)"
    return True, "ok"

# ---------- SYMBOL HELPERS ----------
def pick_hl_symbol(ex: ccxt.Exchange, base: str) -> Optional[str]:
    """
    Find the correct CCXT symbol for Hyperliquid perps with USDC collateral.
    We search loaded markets for:
      - market['swap'] == True (perpetual)
      - market['settle'] == 'USDC'
      - market['base'] == base.upper()
    Then return market['symbol'], e.g. 'BTC/USD:USDC' or 'SOL/USDC:USDC' depending on CCXT version.
    """
    base_up = base.upper()
    for sym, m in ex.markets.items():
        try:
            if m.get("swap") and (m.get("settle") == "USDC") and (m.get("base") == base_up):
                return sym
        except Exception:
            continue
    return None

def fetch_last_price(ex: ccxt.Exchange, symbol: str) -> float:
    """
    Get a reference price for market orders: first from ticker.last then from orderbook.bids/asks.
    """
    t = ex.fetch_ticker(symbol)
    last = t.get("last")
    if last is None:
        ob = ex.fetch_order_book(symbol, limit=5)
        if ob.get("bids"):
            last = ob["bids"][0][0]
        elif ob.get("asks"):
            last = ob["asks"][0][0]
    if last is None:
        raise RuntimeError(f"Could not fetch last price for {symbol}")
    return float(last)

def compute_amount_from_notional(notional: float, px: float, amount_prec: Optional[int]) -> float:
    """
    Convert $notional into contract size (qty). Respects amount precision if provided by CCXT market.
    """
    if px <= 0:
        raise ValueError("Bad reference price")
    raw = notional / px
    if amount_prec is None:
        return float(raw)
    # round down to precision
    factor = 10 ** amount_prec
    return math.floor(raw * factor) / factor

# ---------- ORDER CORE ----------
def place_order(
    ex: ccxt.Exchange,
    symbol: str,
    side: str,                # "buy" | "sell"
    amount: float,
    tif: str,
    post_only: Optional[bool],
    notional: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Market order (with slippage) when price is None; Limit order if post_only True or tif == 'ALO' and price is provided.
    We always pass a reference price for market orders because CCXT Hyperliquid calculates max slippage around it.
    """
    side = side.lower()
    tif = (tif or DEFAULT_TIF).upper()

    # Leverage / tif / post_only pass-through (Hyperliquid reads them in "order params")
    core_params: Dict[str, Any] = {
        "tif": tif,
        "leverage": DEFAULT_LEVERAGE,
    }
    if post_only is True:
        core_params["postOnly"] = True

    # Market: need a price + slippage; Limit: need explicit limit price (we don't do limit here from webhook)
    ref_px = fetch_last_price(ex, symbol)

    # CCXT Hyperliquid requires a 'price' for market orders to compute the slippage band
    # We pass price=ref_px and slippage=DEFAULT_SLIPPAGE in the "globalParams"
    # On recent CCXT builds, passing price for market is supported (used as reference).
    global_params = {"slippage": DEFAULT_SLIPPAGE}

    # Finally create order (market with reference price)
    order = ex.create_order(symbol, "market", side, float(amount), ref_px, {**core_params, **global_params})
    return order

# ---------- FLASK ROUTES ----------
@app.route("/whoami", methods=["GET"])
def whoami():
    try:
        ex = make_exchange()
        # What CCXT is actually configured with
        wallet_addr = getattr(ex, "walletAddress", None) or ex.options.get("walletAddress")
        return jsonify({
            "network": HL_NETWORK,
            "ownerWallet": os.getenv("HL_OWNER_WALLET", ""),  # optional, for your own reference
            "apiWallet_env": HL_API_WALLET,
            "apiWallet_from_privateKey": wallet_addr,
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/health", methods=["GET"])
def health():
    try:
        ex = make_exchange()
        ok, msg = validate_credentials(ex)
        # Load a tiny bit of account info (balance fetch is optional on testnet, but harmless)
        balance = None
        try:
            balance = ex.fetch_balance().get("total", {}).get("USDC")
        except Exception:
            pass
        return jsonify({
            "status": "healthy" if ok else "bad_credentials",
            "network": HL_NETWORK,
            "credentials_set": ok,
            "balance": balance,
            "trading": "active" if ok else "disabled",
            "note": msg,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/markets", methods=["GET"])
def markets():
    try:
        ex = make_exchange()
        # Return a compact view of USDC-settled perps
        data = []
        for sym, m in ex.markets.items():
            if m.get("swap") and m.get("settle") == "USDC":
                data.append({
                    "symbol": sym,
                    "base": m.get("base"),
                    "quote": m.get("quote"),
                    "settle": m.get("settle"),
                    "amountPrecision": m.get("precision", {}).get("amount"),
                    "pricePrecision": m.get("precision", {}).get("price"),
                })
        # Sort for readability
        data.sort(key=lambda x: (x["base"], x["symbol"]))
        return jsonify({"count": len(data), "markets": data})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/", methods=["GET"])
def root():
    return (
        "Hyperliquid TradingView → HL Webhook (CCXT)\n"
        "Endpoints: /health /whoami /markets /webhook/tradingview\n",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
    )

@app.route("/webhook/tradingview", methods=["POST"])
def tradingview():
    """
    Body examples:
    - {"symbol":"BTC","action":"buy","quantity":0.05}
    - {"symbol":"SOL","action":"buy","notional":50,"tif":"IOC"}
    Optional: {"post_only": true, "tif":"ALO"}
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
        sym_in = str(payload.get("symbol", "")).strip().upper()
        action = str(payload.get("action", "buy")).strip().lower()
        tif = str(payload.get("tif", DEFAULT_TIF)).strip().upper()
        post_only = bool(payload.get("post_only", False))

        if action not in ("buy", "sell"):
            return jsonify({"status": "error", "message": "action must be buy|sell"}), 400

        qty = payload.get("quantity")
        notional = payload.get("notional")  # $ amount

        ex = make_exchange()

        # Verify creds one more time
        ok, msg = validate_credentials(ex)
        if not ok:
            return jsonify({"status": "error", "message": msg}), 400

        # Find correct HL symbol in CCXT
        hl_symbol = pick_hl_symbol(ex, sym_in)
        if not hl_symbol:
            return jsonify({"status": "error", "message": f"Could not find USDC-settled perp for base={sym_in}. See /markets."}), 400

        # Pull market precisions for sizing
        mkt = ex.market(hl_symbol)
        amt_prec = (mkt.get("precision") or {}).get("amount")

        # If notional is provided, convert to amount using the live price
        if notional is not None and qty is None:
            ref_px = fetch_last_price(ex, hl_symbol)
            amount = compute_amount_from_notional(float(notional), ref_px, amt_prec)
        elif qty is not None:
            amount = float(qty)
        else:
            return jsonify({"status": "error", "message": "Provide either quantity or notional."}), 400

        order = place_order(
            ex=ex,
            symbol=hl_symbol,
            side=action,
            amount=amount,
            tif=tif,
            post_only=post_only,
            notional=float(notional) if notional is not None else None,
        )

        return jsonify({"status": "ok", "symbol": hl_symbol, "side": action, "amount": amount, "order": order})

    except ccxt.BaseError as e:
        # CCXT exchange-level errors (auth, symbol, etc.)
        return jsonify({"status": "error", "message": f"hyperliquid {str(e)}"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "trace": traceback.format_exc()}), 500


# ---------- GUNICORN ENTRY ----------
# Gunicorn command: gunicorn webhook_server:app -b 0.0.0.0:$PORT
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
