from flask import Flask, request, jsonify
import logging
from hyperliquid_trader import HyperliquidTrader
import os

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize trader
try:
    trader = HyperliquidTrader()
    logger.info("Hyperliquid trader initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Hyperliquid trader: {e}")
    trader = None

def parse_tradingview_alert(alert_data: dict) -> dict:
    """Parse TradingView alert data"""
    try:
        # Handle different alert formats
        if isinstance(alert_data, str):
            import json
            alert_data = json.loads(alert_data)
        
        # Extract trade parameters
        symbol = alert_data.get('symbol', '').upper()
        action = alert_data.get('action', '').lower()
        quantity = float(alert_data.get('quantity', 0))
        price = float(alert_data.get('price', 0))
        order_type = alert_data.get('order_type', 'market').lower()
        
        # Validate required fields
        if not symbol or quantity <= 0:
            raise ValueError("Missing symbol or invalid quantity")
        
        is_buy = action in ['buy', 'long']
        
        return {
            'symbol': symbol,
            'is_buy': is_buy,
            'quantity': quantity,
            'price': price,
            'order_type': order_type
        }
    except Exception as e:
        logger.error(f"Error parsing alert: {e}")
        raise

@app.route('/webhook/tradingview', methods=['POST'])
def tradingview_webhook():
    try:
        if trader is None:
            return jsonify({
                "status": "error",
                "message": "Trader not initialized. Check environment variables."
            }), 500
            
        data = request.get_json()
        if not data:
            return jsonify({
                "status": "error", 
                "message": "No JSON data received"
            }), 400
            
        logger.info(f"Received TradingView alert: {data}")
        
        # Parse the alert
        trade_params = parse_tradingview_alert(data)
        
        # Execute trade based on order type
        if trade_params['order_type'] == 'market':
            result = trader.place_market_order(
                coin=trade_params['symbol'],
                is_buy=trade_params['is_buy'],
                size=trade_params['quantity']
            )
        else:
            result = trader.place_limit_order(
                coin=trade_params['symbol'],
                is_buy=trade_params['is_buy'],
                size=trade_params['quantity'],
                price=trade_params['price']
            )
        
        logger.info(f"Trade executed successfully: {result}")
        
        return jsonify({
            "status": "success",
            "message": "Trade executed",
            "result": result
        }), 200
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 400

@app.route('/health', methods=['GET'])
def health_check():
    status = "healthy" if trader is not None else "unhealthy"
    return jsonify({
        "status": status,
        "account": trader.account_address if trader else "Not configured",
        "network": "testnet" if trader and trader.use_testnet else "mainnet"
    }), 200

@app.route('/balance', methods=['GET'])
def get_balance():
    try:
        if trader is None:
            return jsonify({"error": "Trader not initialized"}), 400
        balance = trader.check_balance()
        return jsonify({"balance": balance}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
