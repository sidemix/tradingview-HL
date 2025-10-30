from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
import os
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

@app.route('/analyze-working-patterns', methods=['GET'])
def analyze_working_patterns():
    """Analyze the patterns from working endpoints"""
    tests = {}
    
    # Test patterns based on working endpoints
    working_patterns = [
        # Pattern from clearinghouseState (works!)
        {"type": "clearinghouseState", "user": WALLET_ADDRESS},
        
        # Maybe order needs similar structure
        {"type": "order", "user": WALLET_ADDRESS},
        
        # Maybe order needs to be in different format
        {"type": "order", "user": WALLET_ADDRESS, "order": {}},
        
        # Try with action field but different structure
        {"action": "placeOrder", "user": WALLET_ADDRESS},
        
        # Try the exact working pattern but for orders
        {"type": "order", "user": WALLET_ADDRESS, "coin": "BTC", "side": "A", "sz": "0.001"},
    ]
    
    for i, payload in enumerate(working_patterns):
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json=payload,
                timeout=10
            )
            tests[f'pattern_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text[:200] if response.text else 'Empty'
            }
        except Exception as e:
            tests[f'pattern_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/test-exchange-with-user', methods=['GET'])
def test_exchange_with_user():
    """Test exchange endpoint with user field like working patterns"""
    tests = {}
    
    # Test exchange endpoint with user field (like clearinghouseState)
    exchange_payloads = [
        # Add user field like working endpoints
        {
            "user": WALLET_ADDRESS,
            "action": {
                "type": "order",
                "orders": [
                    {
                        "coin": "BTC",
                        "side": "A",
                        "sz": "0.001",
                        "order_type": {"market": {}}
                    }
                ]
            }
        },
        
        # Maybe user goes inside action
        {
            "action": {
                "type": "order",
                "user": WALLET_ADDRESS,
                "orders": [
                    {
                        "coin": "BTC",
                        "side": "A", 
                        "sz": "0.001",
                        "order_type": {"market": {}}
                    }
                ]
            }
        },
        
        # Try completely different structure
        {
            "type": "order",
            "user": WALLET_ADDRESS,
            "orders": [
                {
                    "coin": "BTC",
                    "side": "A",
                    "sz": "0.001",
                    "order_type": {"market": {}}
                }
            ]
        },
    ]
    
    for i, payload in enumerate(exchange_payloads):
        try:
            # Sign the request
            message = json.dumps(payload, separators=(',', ':'), sort_keys=True)
            signature = hmac.new(
                bytes(SECRET_KEY, 'utf-8'),
                msg=bytes(message, 'utf-8'),
                digestmod=hashlib.sha256
            ).hexdigest()
            
            headers = {
                "Content-Type": "application/json",
                "X-API-Signature": signature
            }
            
            response = requests.post(
                "https://api.hyperliquid.xyz/exchange",
                json=payload,
                headers=headers,
                timeout=10
            )
            
            tests[f'payload_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text,
                'signature_short': signature[:20] + '...'
            }
            
        except Exception as e:
            tests[f'payload_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/get-account-info', methods=['GET'])
def get_account_info():
    """Get comprehensive account information using working endpoints"""
    account_info = {}
    
    # Get clearinghouse state (works!)
    try:
        clearinghouse_response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": WALLET_ADDRESS},
            timeout=10
        )
        if clearinghouse_response.status_code == 200:
            account_info['clearinghouse'] = clearinghouse_response.json()
    except Exception as e:
        account_info['clearinghouse_error'] = str(e)
    
    # Get open orders (works!)
    try:
        open_orders_response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "openOrders", "user": WALLET_ADDRESS},
            timeout=10
        )
        if open_orders_response.status_code == 200:
            account_info['open_orders'] = open_orders_response.json()
    except Exception as e:
        account_info['open_orders_error'] = str(e)
    
    # Get user fills (might work)
    try:
        user_fills_response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "userFills", "user": WALLET_ADDRESS},
            timeout=10
        )
        account_info['user_fills_status'] = user_fills_response.status_code
        if user_fills_response.status_code == 200:
            account_info['user_fills'] = user_fills_response.json()
        else:
            account_info['user_fills_response'] = user_fills_response.text
    except Exception as e:
        account_info['user_fills_error'] = str(e)
    
    return jsonify(account_info)

@app.route('/test-order-via-info', methods=['GET'])
def test_order_via_info():
    """Test if orders go through info endpoint instead of exchange"""
    tests = {}
    
    # Maybe orders are placed through info endpoint?
    order_payloads = [
        {
            "type": "order",
            "user": WALLET_ADDRESS,
            "order": {
                "coin": "BTC",
                "side": "A",
                "sz": "0.001",
                "order_type": {"market": {}}
            }
        },
        {
            "type": "placeOrder", 
            "user": WALLET_ADDRESS,
            "order": {
                "coin": "BTC",
                "side": "A",
                "sz": "0.001", 
                "order_type": {"market": {}}
            }
        },
    ]
    
    for i, payload in enumerate(order_payloads):
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json=payload,
                timeout=10
            )
            tests[f'info_order_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text
            }
        except Exception as e:
            tests[f'info_order_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/final-order-test', methods=['GET'])
def final_order_test():
    """Final comprehensive order test based on all discoveries"""
    tests = {}
    
    # Based on our discoveries, let's try the most likely formats
    final_payloads = [
        # Format 1: With user field at root (like clearinghouseState)
        {
            "user": WALLET_ADDRESS,
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
        },
        
        # Format 2: User inside action
        {
            "action": {
                "type": "order", 
                "user": WALLET_ADDRESS,
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
        },
        
        # Format 3: Simple format with user
        {
            "user": WALLET_ADDRESS,
            "type": "order",
            "orders": [
                {
                    "coin": "BTC",
                    "side": "A",
                    "sz": "0.001", 
                    "order_type": {"market": {}}
                }
            ]
        },
    ]
    
    for i, payload in enumerate(final_payloads):
        try:
            # Sign the request
            message = json.dumps(payload, separators=(',', ':'), sort_keys=True)
            signature = hmac.new(
                bytes(SECRET_KEY, 'utf-8'),
                msg=bytes(message, 'utf-8'),
                digestmod=hashlib.sha256
            ).hexdigest()
            
            headers = {
                "Content-Type": "application/json",
                "X-API-Signature": signature
            }
            
            # Try both endpoints
            for endpoint in ["/exchange", "/info"]:
                response = requests.post(
                    f"https://api.hyperliquid.xyz{endpoint}",
                    json=payload,
                    headers=headers,
                    timeout=10
                )
                
                tests[f'final_{i}_{endpoint}'] = {
                    'endpoint': endpoint,
                    'payload': payload,
                    'status': response.status_code,
                    'response': response.text,
                    'signature_short': signature[:20] + '...'
                }
                
        except Exception as e:
            tests[f'final_{i}_error'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "Hyperliquid API - Working Endpoints Found!",
        "status": "SUCCESS - Account found with $8218.31 balance",
        "endpoints": {
            "health": "/health",
            "analyze_patterns": "/analyze-working-patterns",
            "test_exchange_user": "/test-exchange-with-user", 
            "account_info": "/get-account-info",
            "test_order_via_info": "/test-order-via-info",
            "final_order_test": "/final-order-test"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
