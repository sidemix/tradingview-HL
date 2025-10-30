from flask import Flask, request, jsonify
import hmac
import hashlib
import json
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
        
        # Execute trade on Hyperliquid
        if side in ['buy', 'sell']:
            result = hl.order(symbol, side == 'buy', size, order_type)
            print(f"Hyperliquid response: {result}")
            
            # Handle different response formats
            if result.get('status') == 'ok' or 'response' in result:
                return jsonify({
                    "status": "success", 
                    "message": f"Order executed: {side} {size} {symbol}",
                    "details": result
                })
            else:
                error_msg = result.get('error', 'Unknown error from Hyperliquid')
                return jsonify({
                    "status": "error",
                    "message": error_msg
                }), 400
        else:
            return jsonify({"error": "Invalid side. Use 'buy' or 'sell'"}), 400
            
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
