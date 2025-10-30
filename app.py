from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
import os
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

def test_basic_connection():
    """Test the most basic API connection"""
    tests = {}
    
    # Test 1: Basic meta endpoint (no auth needed)
    try:
        meta_response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "meta"},
            timeout=10
        )
        tests['meta_endpoint'] = {
            'status': meta_response.status_code,
            'response': meta_response.text[:200] if meta_response.text else 'Empty'
        }
    except Exception as e:
        tests['meta_endpoint'] = {'error': str(e)}
    
    # Test 2: User state with different formats
    user_formats = [
        WALLET_ADDRESS,  # Original
        WALLET_ADDRESS.lower(),  # Lowercase
        WALLET_ADDRESS.upper(),  # Uppercase
    ]
    
    tests['user_state_tests'] = []
    for user_format in user_formats:
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "userState", "user": user_format},
                timeout=10
            )
            tests['user_state_tests'].append({
                'format': user_format,
                'status': response.status_code,
                'response': response.text
            })
        except Exception as e:
            tests['user_state_tests'].append({
                'format': user_format,
                'error': str(e)
            })
    
    return tests

def test_order_signing():
    """Test if our signing is working correctly"""
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
    
    # Test signature generation
    message = json.dumps(test_payload, separators=(',', ':'), sort_keys=True)
    signature = hmac.new(
        bytes(SECRET_KEY, 'utf-8'),
        msg=bytes(message, 'utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    return {
        'message': message,
        'signature': signature,
        'secret_key_length': len(SECRET_KEY),
        'secret_key_prefix': SECRET_KEY[:10] + '...' if SECRET_KEY else 'None'
    }

@app.route('/debug', methods=['GET'])
def debug():
    """Comprehensive debug endpoint"""
    try:
        basic_tests = test_basic_connection()
        signing_test = test_order_signing()
        
        return jsonify({
            'status': 'debug_complete',
            'basic_tests': basic_tests,
            'signing_test': signing_test,
            'env_vars': {
                'wallet_address_set': bool(WALLET_ADDRESS),
                'wallet_address': WALLET_ADDRESS[:10] + '...' if WALLET_ADDRESS else 'None',
                'secret_key_set': bool(SECRET_KEY),
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test-simple-order', methods=['POST'])
def test_simple_order():
    """Test order with minimal parameters"""
    try:
        # Try the absolute simplest possible order
        simple_payload = {
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
        }
        
        # Remove grouping to see if that's the issue
        print(f"Testing simple order: {json.dumps(simple_payload)}")
        
        # Sign the request
        message = json.dumps(simple_payload, separators=(',', ':'), sort_keys=True)
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
            json=simple_payload,
            headers=headers,
            timeout=10
        )
        
        return jsonify({
            'simple_payload': simple_payload,
            'signature': signature,
            'status_code': response.status_code,
            'response': response.text,
            'headers_sent': {'X-API-Signature': 'present'}  # Don't log actual signature
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test-permissions', methods=['GET'])
def test_permissions():
    """Test if API key has correct permissions"""
    try:
        # Try to check what the API key can do
        test_payload = {
            "action": {
                "type": "order"
            }
        }
        
        message = json.dumps(test_payload, separators=(',', ':'), sort_keys=True)
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
            json=test_payload,
            headers=headers,
            timeout=10
        )
        
        return jsonify({
            'status_code': response.status_code,
            'response': response.text
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/debug-user-state', methods=['GET'])
def debug_user_state():
    """Debug user state specifically"""
    tests = {}
    
    # Test different user state payload formats
    test_payloads = [
        {"type": "userState", "user": WALLET_ADDRESS},
        {"type": "userState", "user": WALLET_ADDRESS.lower()},
        {"type": "userState", "user": WALLET_ADDRESS.upper()},
        # Try without 'user' field
        {"type": "userState"},
        # Try with empty user
        {"type": "userState", "user": ""},
    ]
    
    for i, payload in enumerate(test_payloads):
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json=payload,
                timeout=10
            )
            tests[f'test_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text
            }
        except Exception as e:
            tests[f'test_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/test-api-key', methods=['GET'])
def test_api_key():
    """Test if API key has order permissions"""
    try:
        # Try a very simple order to test permissions
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
        
        message = json.dumps(test_payload, separators=(',', ':'), sort_keys=True)
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
            json=test_payload,
            headers=headers,
            timeout=10
        )
        
        return jsonify({
            'status_code': response.status_code,
            'response': response.text,
            'error_analysis': analyze_error(response.text)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def analyze_error(error_text):
    """Analyze common Hyperliquid errors"""
    if "Failed to deserialize" in error_text:
        return "API format error - check payload structure"
    elif "signature" in error_text.lower():
        return "Signature verification failed"
    elif "unauthorized" in error_text.lower():
        return "API key permissions issue"
    elif "user" in error_text.lower():
        return "User/wallet address issue"
    else:
        return "Unknown error - check API key and permissions"

@app.route('/check-wallet', methods=['GET'])
def check_wallet():
    """Check if wallet exists and get basic info"""
    try:
        # Get all user states to see available wallets
        meta_response = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "meta"},
            timeout=10
        )
        
        # Try to get any user info that works
        test_users = [
            WALLET_ADDRESS,
            "0x0000000000000000000000000000000000000000",  # Test with zero address
        ]
        
        results = {}
        for user in test_users:
            try:
                response = requests.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "userState", "user": user},
                    timeout=5
                )
                results[user] = {
                    'status': response.status_code,
                    'response': response.text[:200]
                }
            except:
                results[user] = {'error': 'timeout'}
        
        return jsonify({
            'meta_status': meta_response.status_code,
            'wallet_tests': results,
            'available_coins_count': len(meta_response.json().get('universe', [])) if meta_response.status_code == 200 else 0
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test-networks', methods=['GET'])
def test_networks():
    """Test both mainnet and testnet"""
    networks = {
        'mainnet': 'https://api.hyperliquid.xyz',
        'testnet': 'https://api.hyperliquid-testnet.xyz'
    }
    
    results = {}
    
    for name, url in networks.items():
        try:
            # Test meta endpoint
            meta_response = requests.post(
                f"{url}/info",
                json={"type": "meta"},
                timeout=10
            )
            
            # Test user state
            user_response = requests.post(
                f"{url}/info", 
                json={"type": "userState", "user": WALLET_ADDRESS},
                timeout=10
            )
            
            results[name] = {
                'meta_status': meta_response.status_code,
                'user_state_status': user_response.status_code,
                'meta_response': meta_response.text[:100],
                'user_state_response': user_response.text[:100]
            }
            
        except Exception as e:
            results[name] = {'error': str(e)}
    
    return jsonify(results)

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "TradingView to Hyperliquid Webhook - DEBUG MODE",
        "endpoints": {
            "health": "/health",
            "debug": "/debug",
            "test_simple_order": "/test-simple-order (POST)",
            "test_permissions": "/test-permissions"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
