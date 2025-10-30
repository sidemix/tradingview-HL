# webhook_server.py
from flask import Flask, request, jsonify
import logging, os, json
from dotenv import load_dotenv

# ✔ NEW: Hyperliquid SDK
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

        self.base_url = (
            constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        )

        # ✔ Use SDK clients
        self.info = Info(self.base_url, skip_ws=True)
        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set - running in demo mode")
            self.exchange = None
            self.initialized = False
            return

        try:
            self.exchange = Exchange(self.account_address, self.secret_key, base_url=self.base_url)
            # Smoke test: fetch user state
            state = self.info.user_state(self.account_address.lower())
            balance = float(state.get("withdrawable", 0)) if state else 0.0
            logger.info(f"✅ Hyperliquid initialized! Balance: {balance}")
            self.initialized = True
        except Exception as e:
            logger.exception(f"❌ Failed to initialize Hyperliquid SDK: {e}")
            self.exchange = None
            self.initialized = False

    def name_to_asset(self, coin: str) -> int:
        """Resolve 'BTC' -> asset index using SDK meta (perps)."""
        meta = self.info.meta()
        for idx, a in enumerate(meta["universe"]):
            if a["name"].upper() == coin.upper():
                return idx
        raise ValueError(f"Coin {coin} not found")

    def place_market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool=False):
        """
        Submit a market order using SDK. HL "market" is represented as limit tif IOC with price '0'.
        The SDK handles action canonicalization + signing.
        """
        if not self.exchange:
            return {"error": "Exchange not initialized"}

        # HL min notional is ~$10 – you may enforce that here if desired.
        order_req = {
            "coin": coin.upper(),
            "is_buy": is_buy,
            "sz": str(size),
            "limit_px": "0",              # market via IOC + px 0
            "reduce_only": reduce_only,
            "order_type": {"limit": {"tif": "Ioc"}},
            # Optional: "cloid": "0x..." for idempotency
        }

        logger.info(f"Placing market order via SDK: {coin} {'BUY' if is_buy else 'SELL'} {size}")
        # The SDK takes either a single order or a list; here we submit one.
        resp = self.exchange.order(order_req, grouping="na")
        return resp

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
        # Make parsing resilient, but still prefer proper Content-Type: application/json
        data = request.get_json(silent=True, force=False)
        if not data:
            # Try fallback for misconfigured senders
            try:
                data = json.loads(request.data.decode('utf-8'))
            except Exception:
                return jsonify({"status": "error", "message": "No JSON data received"}), 400

        logger.info(f"Received TradingView alert: {data}")

        # Parse alert
        symbol = str(data.get('symbol', 'BTC')).upper().replace("/USD", "").replace("-PERP", "")
        action = str(data.get('action', 'buy')).lower()
        quantity = float(data.get('quantity', 0.001))
        is_buy = action in ('buy', 'long')

        if not trader.initialized:
            return jsonify({
                "status": "demo",
                "message": f"[DEMO] {symbol} {'BUY' if is_buy else 'SELL'} {quantity}",
                "note": "Hyperliquid not initialized"
            }), 200

        # Execute trade via SDK
        result = trader.place_market_order(symbol, is_buy, quantity)

        # SDK returns {'status': 'ok', 'response': {...}} or raises
        if not isinstance(result, dict) or result.get("status") != "ok":
            return jsonify({
                "status": "error",
                "message": "Trade failed",
                "result": result,
            }), 400

        return jsonify({
            "status": "success",
            "message": f"Trade executed: {symbol} {'BUY' if is_buy else 'SELL'} {quantity}",
            "result": result,
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
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
