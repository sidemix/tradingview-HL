from flask import Flask, request, jsonify
import logging, os, json
from dotenv import load_dotenv

# HL SDK
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# Optional: show installed SDK version in logs
try:
    from importlib.metadata import version as _pkg_version
    HL_SDK_VERSION = _pkg_version("hyperliquid-python-sdk")
except Exception:
    HL_SDK_VERSION = "unknown"

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("webhook_server")

app = Flask(__name__)


class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = (os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
        self.secret_key = (os.getenv("HYPERLIQUID_SECRET_KEY") or "").strip()
        self.base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL

        logger.info(f"HL SDK version: {HL_SDK_VERSION}")
        logger.info(f"HL base_url:    {self.base_url}")
        logger.info(f"HL address:     {self.account_address}")
        logger.info(f"Network:        {'testnet' if self.use_testnet else 'mainnet'}")

        # Always create Info for reads
        self.info = Info(self.base_url, skip_ws=True)

        # Run in demo mode if creds are missing
        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set — running in DEMO mode")
            self.exchange = None
            self.initialized = False
            return

        # Correct constructor: positional (address, secret), keyword (info=…, base_url=…)
        try:
            self.exchange = Exchange(self.account_address, self.secret_key, info=self.info, base_url=self.base_url)
            # Smoke test (balance)
            st = self.info.user_state(self.account_address.lower())
            balance = float(st.get("withdrawable", 0)) if st else 0.0
            logger.info(f"✅ Hyperliquid initialized! Balance: {balance}")
            self.initialized = True
        except TypeError as te:
            logger.error(f"❌ Exchange init TypeError: {te}")
            self.exchange = None
            self.initialized = False
        except Exception as e:
            logger.exception("❌ Exchange init failed")
            self.exchange = None
            self.initialized = False

    @staticmethod
    def _normalize_symbol(sym: str) -> str:
        s = (sym or "BTC").upper().strip()
        s = s.replace("/USD", "").replace("-PERP", "")
        return s

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0

    def market_order(self, coin: str, is_buy: bool, sz: float, slippage: float = 0.01):
        """
        Market-equivalent via SDK convenience:
        - market_open uses aggressive limit + IOC under the hood.
        """
        if not self.exchange:
            return {"status": "error", "message": "Exchange not initialized"}

        coin = self._normalize_symbol(coin)
        try:
            logger.info(f"Submitting market order: {coin} {'BUY' if is_buy else 'SELL'} {sz} (slip={slippage})")
            # market_open(coin, is_buy, sz, cloid, slippage)
            resp = self.exchange.market_open(coin, is_buy, sz, None, slippage)
            return resp
        except Exception as e:
            logger.exception("exchange.market_open failed")
            return {"status": "error", "message": str(e)}


trader = HyperliquidTrader()


@app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    try:
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

        result = trader.market_order(symbol, is_buy, qty)

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
