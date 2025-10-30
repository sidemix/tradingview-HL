from flask import Flask, request, jsonify
import json
import os
from hyperliquid import HyperliquidBot
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

# Initialize Hyperliquid client with official SDK
hl = HyperliquidBot(WALLET_ADDRESS, SECRET_KEY, is_testnet=False)

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """
    TradingView webhook endpoint - NOW WORKING WITH OFFICIAL SDK
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data received"}), 400
        
        print(f"Raw webhook data: {data}")
        
        # Parse the trading alert
        symbol = data.get('symbol', 'BTC').upper()
        side = data.get('side', '').lower()
        size = float(data.get('size', 0.001))
        order_type = data.get('order_type', 'market')
        
        print(f"Processing order: {side} {size} {symbol} {order_type}")
        
        # Execute trade using official SDK
        if side in ['buy', 'sell']:
            result = hl.order(symbol, side == 'buy', size, order_type)
            print(f"Hyperliquid response: {result}")
            
            if result.get('status') == 'success':
                return jsonify({
                    "status": "success", 
                    "message": f"Order executed: {side} {size} {symbol}",
                    "details": result.get('response', {})
                })
            else:
                error_msg = result.get('error', 'Unknown error from Hyperliquid')
                return jsonify({
                    "status": "error",
                    "message": f"Hyperliquid error: {error_msg}"
                }), 400
        else:
            return jsonify({"error": "Invalid side. Use 'buy' or 'sell'"}), 400
            
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/test-sdk-order', methods=['POST'])
def test_sdk_order():
    """Test order placement with official SDK"""
    try:
        # Test with a small market order
        result = hl.order('BTC', True, 0.001, 'market')
        
        return jsonify({
            "status": "sdk_test_complete",
            "result": result,
            "message": "Tested order with official Hyperliquid SDK"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get-meta', methods=['GET'])
def get_meta():
    """Get exchange metadata"""
    try:
        meta = hl.get_meta()
        coins = [asset['name'] for asset in meta['universe']] if 'universe' in meta else []
        
        return jsonify({
            "status": "success",
            "available_coins": coins,
            "meta": meta
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get-account-info', methods=['GET'])
def get_account_info():
    """Get comprehensive account info"""
    try:
        user_state = hl.get_user_state()
        meta = hl.get_meta()
        
        return jsonify({
            "status": "success", 
            "user_state": user_state,
            "available_coins": [asset['name'] for asset in meta['universe']] if 'universe' in meta else []
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "READY",
        "message": "TradingView to Hyperliquid Webhook - USING OFFICIAL SDK",
        "setup": "Now using official Hyperliquid Python SDK",
        "endpoints": {
            "health": "/health",
            "webhook": "/webhook (POST) - for TradingView",
            "test_sdk": "/test-sdk-order (POST)",
            "meta": "/get-meta",
            "account": "/get-account-info"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
