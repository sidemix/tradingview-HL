from flask import Flask, request, jsonify
import logging, os, json
from dotenv import load_dotenv

# Hyperliquid SDK
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")
        self.secret_key = os.getenv("HYPERLIQUID_SECRET_KEY", "")
        self.base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL

        # SDK clients
        self.info = Info(self.base_url, skip_ws=True)

        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set - running in demo mode")
            self.exchange = None
            self.initialized = False
            return

        try:
            self.exchange = Exchange(self.base_url, self.account_address, self.secret_key)
            state = self.info.user_state(self.account_address.lower())
            balance = float(state.get("withdrawable", 0)) if state else 0.0
            logger.info(f"✅ Hyperliquid initialized! Balance: {balance}")
            self.initialized = True
        except Exception as e:
            logger.exception(f"❌ Failed to initialize Hyperliquid SDK: {e}")
            self.exchange = None
            self.initialized = False

    def place_market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool=False):
        if not self.exchange:
            return {"error": "Exchange not initialized"}

        order_req = {
            "coin": coin.upper().replace("/USD", "").replace("-PERP", ""),
            "is_buy": is_buy,
            "sz": str(size),
            "limit_px": "0",                       # market via IOC
            "reduce_only": reduce_only,
            "order_type": {"limit": {"tif": "Ioc"}}
        }
        logger.info(f"Placing order via SDK: {order_req}")
        return self.exchange.order(order_req, grouping="na")

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0

trader = HyperliquidTrader()

@app.route('/webhook/tradingview', methods=['POST'])
def tradingview_webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            # Fallback if sender forgot Content-Type header
            try:
                data = json.loads(request.data.decode('utf-8'))
            except Exception:
                return jsonify({"status": "error", "message": "No JSON data received"}), 400

        logger.info(f"Received TradingView alert: {data}")
        symbol = str(data.get('symbol', 'BTC')).upper()
        action = str(data.get('action', 'buy')).lower()
        qty = float(data.get('quantity', 0.001))
        is_buy = action in ('buy', 'long')

        if not trader.initialized:
            return jsonify({
                "status": "demo",
                "message": f"[DEMO] {symbol} {'BUY' if is_buy else 'SELL'} {qty}",
                "note": "Hyperliquid not initialized"
            }), 200

        result = trader.place_market_order(symbol, is_buy, qty)

        if not isinstance(result, dict) or result.get("status") != "ok":
            return jsonify({"status": "error", "message": "Trade failed", "result": result}), 400

        return jsonify({
            "status": "success",
            "message": f"Trade executed: {symbol} {'BUY' if is_buy else 'SELL'} {qty}",
            "result": result
        }), 200

    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "trading": "active" if trader.initialized else "demo",
        "balance": trader.get_balance(),
        "credentials_set": bool(trader.account_address and trader.secret_key),
        "network": "testnet" if trader.use_testnet else "mainnet"
    }), 200

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "TradingView ➜ Hyperliquid Webhook Server (SDK)",
        "endpoints": {"health": "/health (GET)", "webhook": "/webhook/tradingview (POST)"},
        "status": "ACTIVE"
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
