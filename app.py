from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
from hyperliquid import Hyperliquid
import os
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

# Initialize Hyperliquid client
hl = Hyperliquid(WALLET_ADDRESS, SECRET_KEY)

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        # Get raw data first for debugging
        raw_data = request.get_data(as_text=True)
        print(f"Raw data received: {raw_data}")
        
        if not raw_data or raw_data.strip() == '':
            return jsonify({"error": "Empty request body"}), 400
        
        # Try to parse JSON
        try:
            data = request.get_json()
        except Exception as e:
            print(f"JSON parse error: {str(e)}")
            return jsonify({"error": f"Invalid JSON: {str(e)}"}), 400
        
        if data is None:
            return jsonify({"error": "No JSON data received"}), 400
        
        print(f"Parsed data: {data}")
        
        # Parse the trading alert with defaults
        symbol = data.get('symbol', 'BTC').upper()
        side = data.get('side', '').lower()
        size = float(data.get('size', 0.01))
        order_type = data.get('order_type', 'market')
        
        print(f"Processing order: {side} {size} {symbol}")
        
        # Get available coins to verify symbol
        available_coins = hl.get_available_coins()
        print(f"Available coins: {available_coins}")
        
        # Check if symbol exists in available coins
        target_coin = None
        for coin in available_coins:
            if symbol.upper() == coin.upper():
                target_coin = coin
                break
        
        if not target_coin:
            return jsonify({
                "status": "error",
                "message": f"Symbol '{symbol}' not found. Available coins: {available_coins}"
            }), 400
        
        print(f"Using coin: {target_coin}")
        
        # Execute trade on Hyperliquid
        if side in ['buy', 'sell']:
            result = hl.order(target_coin, side == 'buy', size, order_type)
            print(f"Hyperliquid response: {result}")
            
            if result.get('status') == 'success':
                return jsonify({
                    "status": "success", 
                    "message": f"Order executed: {side} {size} {target_coin}",
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

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/test', methods=['GET'])
def test_connection():
    """Test Hyperliquid connection and get user state"""
    try:
        user_state = hl.get_user_state()
        available_coins = hl.get_available_coins()
        return jsonify({
            "status": "success",
            "user_state": user_state,
            "available_coins": available_coins
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route('/test-order', methods=['POST'])
def test_order():
    """Test order with exact format"""
    try:
        # Test with a simple market order
        test_payload = {
            "action": {
                "type": "order", 
                "orders": [
                    {
                        "coin": "BTC",
                        "side": "A",
                        "sz": "0.001",
                        "order_type": {"market": {}}
                    }
                ],
                "grouping": "na"
            }
        }
        
        print(f"Testing with payload: {json.dumps(test_payload, indent=2)}")
        
        # Sign the test payload
        signature = hmac.new(
            bytes(SECRET_KEY, 'utf-8'),
            msg=bytes(json.dumps(test_payload, separators=(',', ':'), sort_keys=True), 'utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        headers = {
            "Content-Type": "application/json",
            "X-API-Signature": signature
        }
        
        response = requests.post(
            "https://api.hyperliquid.xyz/exchange",
            json=test_payload,
            headers=headers,
            timeout=10
        )
        
        return jsonify({
            "status": response.status_code,
            "response": response.text,
            "test_payload": test_payload,
            "signature": signature
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/test-user-state', methods=['GET'])
def test_user_state():
    """Test user state endpoint specifically"""
    try:
        # Test user state with exact format
        user_state_payload = {
            "type": "userState",
            "user": WALLET_ADDRESS
        }
        
        print(f"Testing user state with: {user_state_payload}")
        
        # Info endpoints don't need signatures
        response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json=user_state_payload,
            timeout=10
        )
        
        return jsonify({
            "status": response.status_code,
            "response": response.text,
            "payload": user_state_payload
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "TradingView to Hyperliquid Webhook",
        "endpoints": {
            "health": "/health",
            "test_connection": "/test",
            "test_order": "/test-order (POST)", 
            "test_user_state": "/test-user-state",
            "webhook": "/webhook (POST)"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
