from flask import Flask, request, jsonify
import logging
import os
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
        self.secret_key = os.getenv("HYPERLIQUID_SECRET_KEY")
        
        logger.info(f"Credentials check - Address: {self.account_address}, Secret set: {bool(self.secret_key)}")
        
        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set - running in demo mode")
            self.exchange = None
            self.info = None
            return
        
        try:
            base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
            logger.info(f"Initializing with base_url: {base_url}")
            
            self.info = Info(base_url, skip_ws=True)
            
            # CORRECT Exchange initialization based on SDK source
            # The Exchange class takes: (wallet, key, base_url, account_address)
            self.exchange = Exchange(
                self.account_address,  # wallet
                self.secret_key,       # key (private key)
                base_url=base_url,
                account_address=self.account_address  # might be needed for some operations
            )
            
            # Test the connection by getting user state
            user_state = self.info.user_state(self.account_address)
            balance = user_state.get('withdrawable', 0)
            logger.info(f"Hyperliquid initialized successfully. Balance: {balance}")
            
        except Exception as e:
            logger.error(f"Failed to initialize Hyperliquid: {e}")
            self.exchange = None
            self.info = None

    def place_market_order(self, coin: str, is_buy: bool, size: float):
        if not self.exchange:
            raise Exception("Hyperliquid not configured")
        
        try:
            # Place market order (limit_px=0 for market)
            result = self.exchange.order(coin, is_buy, size, 0, {"limit": {"tif": "Gtc"}})
            logger.info(f"Order placed: {result}")
            return result
        except Exception as e:
            logger.error(f"Order failed: {e}")
            raise

    def get_balance(self):
        if not self.info:
            return 0
        try:
            user_state = self.info.user_state(self.account_address)
            return float(user_state["withdrawable"])
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return 0

# Initialize trader
trader = HyperliquidTrader()

@app.route('/webhook/tradingview', methods=['POST'])
def tradingview_webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400
            
        logger.info(f"Received TradingView alert: {data}")
        
        # Parse alert
        symbol = data.get('symbol', 'BTC').upper()
        action = data.get('action', 'buy').lower()
        quantity = float(data.get('quantity', 0.001))
        order_type = data.get('order_type', 'market')
        
        is_buy = action in ['buy', 'long']
        
        if trader.exchange is None:
            return jsonify({
                "status": "demo",
                "message": f"Alert received: {symbol} {'BUY' if is_buy else 'SELL'} {quantity}",
                "credentials_provided": True,
                "note": "Exchange initialization failed - check deployment logs"
            }), 200
        
        # Execute trade
        if order_type == 'market':
            result = trader.place_market_order(symbol, is_buy, quantity)
        else:
            price = float(data.get('price', 0))
            if price <= 0:
                return jsonify({"status": "error", "message": "Price required for limit orders"}), 400
            result = trader.exchange.order(symbol, is_buy, quantity, price, {"limit": {"tif": "Gtc"}})
        
        return jsonify({
            "status": "success", 
            "message": f"Trade executed: {symbol} {'BUY' if is_buy else 'SELL'} {quantity}",
            "result": result
        }), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/health', methods=['GET'])
def health_check():
    trading_status = "active" if trader.exchange else "demo"
    balance = trader.get_balance()
    credentials_set = bool(trader.account_address and trader.secret_key)
    
    return jsonify({
        "status": "healthy",
        "trading": trading_status,
        "balance": balance,
        "credentials_set": credentials_set,
        "network": "testnet" if trader.use_testnet else "mainnet"
    }), 200

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "TradingView to Hyperliquid Webhook Server",
        "endpoints": {
            "health": "/health (GET)",
            "webhook": "/webhook/tradingview (POST)"
        }
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
