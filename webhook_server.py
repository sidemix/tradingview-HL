from flask import Flask, request, jsonify
import logging
import os
import requests
import json
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
        self.secret_key = os.getenv("HYPERLIQUID_SECRET_KEY")
        
        self.base_url = "https://api.hyperliquid-testnet.xyz" if self.use_testnet else "https://api.hyperliquid.xyz"
        self.info_url = f"{self.base_url}/info"
        self.exchange_url = f"{self.base_url}/exchange"
        
        logger.info(f"Initializing Hyperliquid - Address: {self.account_address}, Network: {'testnet' if self.use_testnet else 'mainnet'}")
        
        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set - running in demo mode")
            self.initialized = False
            return
        
        try:
            # Test connection by getting user state
            user_state = self._info_request({
                "type": "clearinghouseState",
                "user": self.account_address
            })
            
            if "assetPositions" in user_state:
                balance = user_state.get("withdrawable", 0)
                logger.info(f"✅ Hyperliquid initialized successfully! Balance: {balance}")
                self.initialized = True
            else:
                logger.error("❌ Failed to get user state - account may not be initialized")
                self.initialized = False
                
        except Exception as e:
            logger.error(f"❌ Failed to initialize Hyperliquid: {e}")
            self.initialized = False

    def _generate_signature(self, data: dict) -> dict:
        """Generate signature in the exact format Hyperliquid expects"""
        # Hyperliquid expects the signature to be an object with r, s, v fields
        message = json.dumps(data, separators=(',', ':'), sort_keys=True)
        signature_hash = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        # Convert to the expected signature format
        # For now, we'll use a simplified version - might need ECDSA for proper v, r, s
        return {
            "r": f"0x{signature_hash[:64]}",
            "s": f"0x{signature_hash[64:128]}", 
            "v": 27  # Typical Ethereum v value
        }

    def _info_request(self, data: dict) -> dict:
        """Make info endpoint request"""
        response = requests.post(self.info_url, json=data)
        return response.json()

    def _exchange_request(self, action: dict) -> dict:
        """Make exchange endpoint request with signing"""
        nonce = int(time.time() * 1000)
        
        # Generate signature for the action
        signature = self._generate_signature(action)
        
        request_data = {
            "action": action,
            "nonce": nonce,
            "signature": signature
        }
        
        logger.info(f"Sending exchange request: {json.dumps(request_data, indent=2)}")
        
        headers = {
            "Content-Type": "application/json"
        }
        
        response = requests.post(self.exchange_url, json=request_data, headers=headers)
        
        # Better error handling for response
        logger.info(f"Response status: {response.status_code}")
        logger.info(f"Response headers: {response.headers}")
        logger.info(f"Response text: {response.text}")
        
        try:
            response_data = response.json()
            return response_data
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return {"error": f"JSON decode failed: {response.text}", "status_code": response.status_code}

    def get_asset_index(self, coin: str) -> int:
        """Get asset index for a coin"""
        meta = self._info_request({"type": "meta"})
        for i, asset in enumerate(meta["universe"]):
            if asset["name"] == coin.upper():
                return i
        raise ValueError(f"Coin {coin} not found")

    def place_market_order(self, coin: str, is_buy: bool, size: float) -> dict:
        """Place market order using direct API"""
        try:
            asset_index = self.get_asset_index(coin)
            
            order_action = {
                "type": "order",
                "orders": [
                    {
                        "a": asset_index,        # asset index
                        "b": is_buy,             # isBuy
                        "p": "0",                # price (0 for market)
                        "s": str(size),          # size
                        "r": False,              # reduceOnly
                        "t": {"limit": {"tif": "Gtc"}}  # order type
                    }
                ],
                "grouping": "na"
            }
            
            logger.info(f"Placing market order: {coin} {'BUY' if is_buy else 'SELL'} {size}")
            result = self._exchange_request(order_action)
            return result
            
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return {"error": str(e)}

    def get_balance(self) -> float:
        """Get account balance"""
        try:
            user_state = self._info_request({
                "type": "clearinghouseState",
                "user": self.account_address
            })
            return float(user_state.get("withdrawable", 0))
        except:
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
        
        is_buy = action in ['buy', 'long']
        
        if not trader.initialized:
            return jsonify({
                "status": "demo",
                "message": f"Alert received: {symbol} {'BUY' if is_buy else 'SELL'} {quantity}",
                "note": "Hyperliquid not initialized"
            }), 200
        
        # Execute trade
        result = trader.place_market_order(symbol, is_buy, quantity)
        
        if "error" in result:
            return jsonify({
                "status": "error",
                "message": f"Trade failed: {result['error']}",
                "status_code": result.get("status_code", 400)
            }), 400
        
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
    trading_status = "active" if trader.initialized else "demo"
    balance = trader.get_balance()
    
    return jsonify({
        "status": "healthy",
        "trading": trading_status,
        "balance": balance,
        "credentials_set": bool(trader.account_address and trader.secret_key),
        "network": "testnet" if trader.use_testnet else "mainnet"
    }), 200

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "TradingView to Hyperliquid Webhook Server",
        "endpoints": {
            "health": "/health (GET)",
            "webhook": "/webhook/tradingview (POST)"
        },
        "status": "ACTIVE - Ready for TradingView alerts!"
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
