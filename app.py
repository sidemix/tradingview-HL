from flask import Flask, request, jsonify
import json
import os
from hyperliquid import HyperliquidDirect
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

# Initialize Hyperliquid client with CORRECT API format
hl = HyperliquidDirect(WALLET_ADDRESS, SECRET_KEY)

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """
    TradingView webhook endpoint - NOW WITH CORRECT API FORMAT
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
        
        # Execute trade with CORRECT API format
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

@app.route('/test-order', methods=['POST'])
def test_order():
    """Test order placement with CORRECT API format"""
    try:
        # Test with a small market order
        result = hl.order('BTC', True, 0.001, 'market')
        
        return jsonify({
            "status": "test_complete",
            "result": result,
            "message": "Tested order with CORRECT Hyperliquid API format"
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
            "meta_sample": meta['universe'][:3] if 'universe' in meta else []
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

@app.route('/debug-order', methods=['POST'])
def debug_order():
    """Debug order with detailed logging"""
    try:
        test_data = {
            "symbol": "BTC",
            "side": "buy", 
            "size": 0.001,
            "order_type": "market"
        }
        
        print("=== DEBUG ORDER START ===")
        result = hl.order('BTC', True, 0.001, 'market')
        print("=== DEBUG ORDER END ===")
        
        return jsonify({
            "status": "debug_complete",
            "test_data": test_data,
            "result": result
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
        "message": "TradingView to Hyperliquid Webhook - CORRECT API FORMAT",
        "breakthrough": "Found official API documentation with required nonce field",
        "key_changes": [
            "Added required 'nonce' field (timestamp in ms)",
            "Using asset indices instead of coin names", 
            "Correct single-letter field names (a, b, s, p, r, t)",
            "Proper signature covering complete payload"
        ],
        "endpoints": {
            "health": "/health",
            "webhook": "/webhook (POST) - for TradingView",
            "test_order": "/test-order (POST)",
            "debug_order": "/debug-order (POST)",
            "meta": "/get-meta",
            "account": "/get-account-info"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
