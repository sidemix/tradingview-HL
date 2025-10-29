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

def verify_tradingview_webhook(data, signature):
    """
    Verify the webhook came from TradingView
    You can add your own secret verification here
    """
    # Implement your verification logic here
    return True

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        # Get the webhook data
        data = request.get_json()
        
        # Verify the webhook (optional but recommended)
        signature = request.headers.get('X-Signature')
        if not verify_tradingview_webhook(data, signature):
            return jsonify({"error": "Invalid signature"}), 401
        
        # Parse the trading alert
        symbol = data.get('symbol', 'BTC').upper()
        side = data.get('side', '').lower()
        size = float(data.get('size', 0.01))
        order_type = data.get('order_type', 'market')
        
        print(f"Received order: {side} {size} {symbol}")
        
        # Execute trade on Hyperliquid
        if side in ['buy', 'sell']:
            result = hl.order(symbol, side == 'buy', size, order_type)
            
            if result.get('status') == 'ok':
                return jsonify({
                    "status": "success",
                    "message": f"Order executed: {side} {size} {symbol}",
                    "order_id": result.get('order_id')
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": result.get('error', 'Unknown error')
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
