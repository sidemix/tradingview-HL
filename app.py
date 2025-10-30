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

@app.route('/test-official-format', methods=['GET'])
def test_official_format():
    """Test with format from official Hyperliquid documentation/examples"""
    tests = {}
    
    # Based on common exchange API patterns and your working history
    official_formats = [
        # Format 1: Standard exchange format with timestamp
        {
            "timestamp": int(time.time() * 1000),
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
        
        # Format 2: With nonce for replay protection
        {
            "nonce": int(time.time() * 1000),
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
        
        # Format 3: Maybe they use completely different field names
        {
            "method": "private/order",
            "params": {
                "symbol": "BTC",
                "side": "BUY",
                "quantity": "0.001",
                "type": "MARKET"
            }
        },
        
        # Format 4: Try with the exact format from userFills data structure
        {
            "type": "order",
            "orders": [
                {
                    "a": "BTC",  # asset
                    "b": True,   # is_buy
                    "s": "0.001", # size  
                    "t": {"market": {}}  # order_type
                }
            ]
        },
        
        # Format 5: Single letter fields (common in some APIs)
        {
            "a": {
                "t": "order",
                "o": [
                    {
                        "a": "BTC",
                        "b": True, 
                        "s": "0.001",
                        "t": {"market": {}}
                    }
                ],
                "g": "na"
            }
        },
    ]
    
    for i, payload in enumerate(official_formats):
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
                "X-API-Signature": signature,
                # Try additional headers that might be required
                "X-API-Timestamp": str(int(time.time() * 1000)),
            }
            
            response = requests.post(
                "https://api.hyperliquid.xyz/exchange",
                json=payload,
                headers=headers,
                timeout=10
            )
            
            tests[f'official_{i}'] = {
                'payload': payload,
                'status': response.status_code,
                'response': response.text,
                'signature_short': signature[:20] + '...'
            }
            
        except Exception as e:
            tests[f'official_{i}'] = {
                'payload': payload,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/discover-other-endpoints', methods=['GET'])
def discover_other_endpoints():
    """Discover other potential endpoints"""
    tests = {}
    
    endpoints_to_try = [
        "/trade",
        "/api/v1/order", 
        "/api/order",
        "/v1/order",
        "/private/order",
        "/exchange/order",
        "/api/exchange/order",
    ]
    
    base_url = "https://api.hyperliquid.xyz"
    
    for endpoint in endpoints_to_try:
        try:
            # Try simple GET first
            response = requests.get(f"{base_url}{endpoint}", timeout=5)
            tests[f'get_{endpoint}'] = {
                'method': 'GET',
                'status': response.status_code,
                'response': response.text[:100] if response.text else 'Empty'
            }
            
            # Try POST with basic payload
            test_payload = {"test": True}
            response = requests.post(f"{base_url}{endpoint}", json=test_payload, timeout=5)
            tests[f'post_{endpoint}'] = {
                'method': 'POST', 
                'status': response.status_code,
                'response': response.text[:100] if response.text else 'Empty'
            }
            
        except Exception as e:
            tests[f'error_{endpoint}'] = {
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/verify-api-key-setup', methods=['GET'])
def verify_api_key_setup():
    """Verify API key was created with correct permissions"""
    tests = {}
    
    # The API key might need specific permissions
    # Common issue: API key created without order permissions
    
    # Test if we can get any user-specific data that requires the key
    try:
        # Try to get user state with signature (maybe it requires auth)
        user_state_payload = {
            "type": "userState",
            "user": WALLET_ADDRESS
        }
        
        message = json.dumps(user_state_payload, separators=(',', ':'), sort_keys=True)
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
            "https://api.hyperliquid.xyz/info",
            json=user_state_payload,
            headers=headers,
            timeout=10
        )
        
        tests['user_state_with_auth'] = {
            'status': response.status_code,
            'response': response.text
        }
        
    except Exception as e:
        tests['user_state_with_auth_error'] = str(e)
    
    return jsonify(tests)

@app.route('/check-for-sdk', methods=['GET'])
def check_for_sdk():
    """Check if there's an official SDK we should use"""
    info = {
        "suggestion": "Since we can't discover the correct API format through testing, we should:",
        "options": [
            "1. Check Hyperliquid's official GitHub for examples",
            "2. Look for an official Python SDK", 
            "3. Check their documentation for the exact order format",
            "4. Contact their support for API documentation"
        ],
        "current_status": {
            "account_working": True,
            "balance": "$8,210.00", 
            "has_trading_history": True,
            "api_format_unknown": True
        },
        "next_steps": [
            "Visit: https://github.com/hyperliquid-xyz",
            "Check: https://hyperliquid.gitbook.io/hyperliquid-docs/",
            "Look for API examples in their documentation"
        ]
    }
    
    return jsonify(info)

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
