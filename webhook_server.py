# webhook_server.py
from flask import Flask, request, jsonify
import logging
import os
import json
from dotenv import load_dotenv

# Hyperliquid SDK
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
logger = logging.getLogger("webhook_server")

app = Flask(__name__)


class HyperliquidTrader:
    def __init__(self):
        # Env
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
        self.secret_key = os.getenv("HYPERLIQUID_SECRET_KEY", "").strip()

        # Base URL from SDK constants (never from env)
        self.base_url = (
            constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        )

        logger.info(f"HL base_url: {self.base_url}")
        logger.info(f"HL address:  {self.account_address}")
        logger.info(f"Network:     {'testnet' if self.use_testnet else 'mainnet'}")

        # SDK Info client (HTTP only; no WS)
        self.info = Info(self.base_url, skip_ws=True)

        # If creds missing, run in demo mode
        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set - running in DEMO mode")
            self.exchange = None
            self.initialized = False
            return

        # Guard base_url format
        if not isinstance(self.base_url, str) or not self.base_url.startswith("http"):
            logger.error(f"BAD base_url detected: {self.base_url}")
            self.exchange = None
            self.initialized = False
            return

        # Try correct constructor first; fall back if SDK variant differs
        try:
            # ✅ Current SDK: base_url first
            self.exchange = Exchange(self.base_url, self.account_address, self.secret_key)
            state = self.info.user_state(self.account_address.lower())
            balance = float(state.get("withdrawable", 0)) if state else 0.0
            logger.info(f"✅ Hyperliquid initialized! Balance: {balance}")
            self.initialized = True
        except TypeError as te:
            logger.warning(f"TypeError on Exchange init ({te}). Trying alt signature...")
            try:
                # Older SDK variant
                self.exchange = Exchange(self.account_address, self.secret_key, base_url=self.base_url)
                state = self.info.user_state(self.account_address.lower())
                balance = float(state.get("withdrawable", 0)) if state else 0.0
                logger.info(f"✅ Hyperliquid initialized (alt ctor)! Balance: {balance}")
                self.initialized = True
            except Exception as e2:
                logger.exception(f"❌ Failed alt ctor as well: {e2}")
                self.exchange = None
                self.initialized = False
        except Exception as e:
            logger.exception(f"❌ Failed to initialize Hyperliquid SDK: {e}")
            self.exchange = None
            self.initialized = False

    @staticmethod
    def _normalize_symbol(sym: str) -> str:
        # Accept BTC / BTC/USD / BTC-PERP and normalize to 'BTC'
        s = (sym or "BTC").upper().strip()
        s = s.replace("/USD", "").replace("-PERP", "")
        return s

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0

    def place_market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False):
        """Place a market-equivalent order: limit_px='0' + IOC."""
        if not self.exchange:
            return {"status": "error", "message": "Exchange not initialized"}

        coin = self._normalize_symbol(coin)

        # HL enforces min notional (~$10); caller should choose size accordingly.
        order_req = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": str(size),                  # send as string per HL examples
            "limit_px": "0",                  # '0' with IOC = market
            "reduce_only": reduce_only,
            "order_type": {"limit": {"tif": "Ioc"}},
            # Optional: "cloid": "0x..." for idempotency
        }

        logger.info(f"Submitting order via SDK: {order_req}")
        try:
            resp = self.exchange.order(order_req, grouping="na")
            return resp
        except Exception as e:
            logger.exception("Exchange.order failed")
            return {"status": "error", "message": str(e)}


trader = HyperliquidTrader()


# ---------- Routes ----------
@app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    try:
        # Prefer proper JSON header; be tolerant if missing.
        data = request.get_json(silent=True)
        if not data:
            try:
                data = json.loads(request.data.decode("utf-8"))
            except Exception:
                return jsonify({"status": "error", "message": "No JSON data received"}), 400

        logger.info(f"Received TradingView alert: {data}")

        symbol = str(data.get("symbol", "BTC"))
        action = str(data.get("action", "buy")).lower()
        qty = float(data.get("quantity", 0.001))

        is_buy = action in ("buy", "long")

        if not trader.initialized:
            return jsonify({
                "status": "demo",
                "message": f"[DEMO] {trader._normalize_symbol(symbol)} {'BUY' if is_buy else 'SELL'} {qty}",
                "note": "Hyperliquid not initialized"
            }), 200

        result = trader.place_market_order(symbol, is_buy, qty)

        # SDK typically returns {"status":"ok", "response": {...}} on success
        if isinstance(result, dict) and result.get("status") == "ok":
            return jsonify({
                "status": "success",
                "message": f"Trade executed: {trader._normalize_symbol(symbol)} {'BUY' if is_buy else 'SELL'} {qty}",
                "result": result
            }), 200

        return jsonify({
            "status": "error",
            "message": "Trade failed",
            "result": result
        }), 400

    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "trading": "active" if trader.initialized else "demo",
        "balance": trader.get_balance(),
        "credentials_set": bool(trader.account_address and trader.secret_key),
        "network": "testnet" if trader.use_testnet else "mainnet"
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "TradingView ➜ Hyperliquid Webhook Server (SDK)",
        "endpoints": {"health": "/health (GET)", "webhook": "/webhook/tradingview (POST)"},
        "status": "ACTIVE"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
