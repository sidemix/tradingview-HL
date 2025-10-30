from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
import os
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

@app.route('/discover-user-state', methods=['GET'])
def discover_user_state():
    """Discover the correct user state format"""
    tests = {}
    
    # Test different user state formats
    user_state_formats = [
        # Current failing format
        {"type": "userState", "user": WALLET_ADDRESS},
        
        # Maybe they expect different field names
        {"type": "userState", "address": WALLET_ADDRESS},
        {"type": "userState", "wallet": WALLET_ADDRESS},
        {"type": "userState", "account": WALLET_ADDRESS},
        
        # Maybe user should be an object
        {"type": "userState", "user": {"address": WALLET_ADDRESS}},
        
        # Maybe different type values
        {"type": "user", "user": WALLET_ADDRESS},
        {"type": "account", "user": WALLET_ADDRESS},
        {"type": "getUserState", "user": WALLET_ADDRESS},
        
        # Maybe it needs additional fields
        {"type": "userState", "user": WALLET_ADDRESS, "method": "info"},
        
        # Try with signature (maybe user state needs auth)
        {"type": "userState", "user": WALLET_ADDRESS, "signature": "test"},
    ]
    
    for i, payload in enumerate(user_state_formats):
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json=payload,
                timeout=10
            )
            tests[f'format_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text[:200] if response.text else 'Empty'
            }
        except Exception as e:
            tests[f'format_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/discover-exchange-format', methods=['GET'])
def discover_exchange_format():
    """Discover the correct exchange/order format"""
    tests = {}
    
    # Test different exchange action formats
    exchange_formats = [
        # Maybe the action structure is wrong
        {
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
        
        # Maybe no nested action
        {
            "method": "order",
            "orders": [
                {
                    "coin": "BTC", 
                    "side": "A",
                    "sz": "0.001",
                    "order_type": {"market": {}}
                }
            ]
        },
        
        # Maybe different order structure
        {
            "action": "order",
            "order": {
                "coin": "BTC",
                "side": "A", 
                "sz": "0.001",
                "order_type": {"market": {}}
            }
        },
        
        # Maybe it needs is_buy instead of side
        {
            "action": {
                "type": "order",
                "orders": [
                    {
                        "coin": "BTC",
                        "is_buy": True,
                        "sz": "0.001", 
                        "order_type": {"market": {}}
                    }
                ]
            }
        },
        
        # Try with limit order first (maybe market has issues)
        {
            "action": {
                "type": "order", 
                "orders": [
                    {
                        "coin": "BTC",
                        "side": "A",
                        "sz": "0.001",
                        "limit_px": "50000",
                        "order_type": {"limit": {"tif": "Gtc"}}
                    }
                ]
            }
        },
    ]
    
    for i, payload in enumerate(exchange_formats):
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
            
            tests[f'format_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text,
                'signature_short': signature[:20] + '...'
            }
            
        except Exception as e:
            tests[f'format_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/test-with-other-actions', methods=['GET'])
def test_with_other_actions():
    """Test other exchange actions to understand the pattern"""
    tests = {}
    
    # Test different action types that might work
    action_types = [
        {"action": {"type": "info"}},
        {"action": {"type": "balance"}},
        {"action": {"type": "positions"}},
        {"action": {"type": "openOrders"}},
        {"action": {"type": "cancelAll"}},
    ]
    
    for i, payload in enumerate(action_types):
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
            
            tests[f'action_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text
            }
            
        except Exception as e:
            tests[f'action_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/check-api-keys', methods=['GET'])
def check_api_keys():
    """Verify API keys are working by testing signature"""
    tests = {}
    
    # Test if we can make any authenticated request
    test_payloads = [
        # Maybe we can get balance or something simple
        {"action": {"type": "balance"}},
        {"action": {"type": "userData"}},
        # Empty action to see what error we get
        {"action": {}},
    ]
    
    for i, payload in enumerate(test_payloads):
        try:
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
            
            tests[f'test_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text,
                'signature_valid': signature[:10] + '...'
            }
            
        except Exception as e:
            tests[f'test_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/test-without-signature', methods=['GET'])
def test_without_signature():
    """Test if endpoints work without signature (public endpoints)"""
    tests = {}
    
    # Test various endpoints without authentication
    endpoints = [
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "meta"}},
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "userState", "user": WALLET_ADDRESS}},
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "clearinghouseState", "user": WALLET_ADDRESS}},
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "openOrders", "user": WALLET_ADDRESS}},
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "allMids"}},
        {"url": "https://api.hyperliquid.xyz/info", "payload": {"type": "candleSnapshot", "coin": "BTC", "interval": "1h"}},
    ]
    
    for i, endpoint in enumerate(endpoints):
        try:
            response = requests.post(
                endpoint["url"],
                json=endpoint["payload"],
                timeout=10
            )
            
            tests[f'endpoint_{i}'] = {
                'url': endpoint["url"],
                'payload': endpoint["payload"],
                'status': response.status_code,
                'response': response.text[:200] if response.text else 'Empty'
            }
            
        except Exception as e:
            tests[f'endpoint_{i}'] = {
                'url': endpoint["url"],
                'payload': endpoint["payload"],
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "Hyperliquid API Format Discovery",
        "endpoints": {
            "health": "/health",
            "discover_user_state": "/discover-user-state", 
            "discover_exchange": "/discover-exchange-format",
            "test_other_actions": "/test-with-other-actions",
            "check_api_keys": "/check-api-keys",
            "test_without_signature": "/test-without-signature"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
