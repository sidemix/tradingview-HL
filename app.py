from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import requests
import os
from config import SECRET_KEY, WALLET_ADDRESS

app = Flask(__name__)

@app.route('/discover-api', methods=['GET'])
def discover_api():
    """Discover the correct Hyperliquid API format"""
    tests = {}
    
    # Test different base URLs and endpoints
    base_urls = [
        "https://api.hyperliquid.xyz",
        "https://api.hyperliquid.xyz/api",
        "https://api.hyperliquid.xyz/v1",
        "https://hyperliquid.xyz/api",
    ]
    
    # Test different HTTP methods
    methods = ['GET', 'POST']
    
    # Test different content types
    content_types = [
        'application/json',
        'application/x-www-form-urlencoded'
    ]
    
    for base_url in base_urls:
        tests[base_url] = {}
        
        for method in methods:
            tests[base_url][method] = {}
            
            for content_type in content_types:
                try:
                    # Test meta endpoint
                    if method == 'GET':
                        response = requests.get(
                            f"{base_url}/info",
                            headers={'Content-Type': content_type},
                            timeout=5
                        )
                    else:
                        response = requests.post(
                            f"{base_url}/info",
                            json={"type": "meta"},
                            headers={'Content-Type': content_type},
                            timeout=5
                        )
                    
                    tests[base_url][method][content_type] = {
                        'status': response.status_code,
                        'response_preview': response.text[:100] if response.text else 'Empty'
                    }
                    
                except Exception as e:
                    tests[base_url][method][content_type] = {
                        'error': str(e)
                    }
    
    return jsonify(tests)

@app.route('/test-raw-http', methods=['GET'])
def test_raw_http():
    """Test raw HTTP requests to understand the API"""
    import http.client
    import ssl
    
    tests = {}
    
    # Test with raw HTTP connection
    try:
        # Create connection
        conn = http.client.HTTPSConnection("api.hyperliquid.xyz", context=ssl._create_unverified_context())
        
        # Test 1: GET request to /info
        conn.request("GET", "/info", headers={'Content-Type': 'application/json'})
        response1 = conn.getresponse()
        tests['get_info'] = {
            'status': response1.status,
            'headers': dict(response1.getheaders()),
            'body': response1.read().decode()[:200]
        }
        
        # Test 2: POST request to /info
        headers = {'Content-Type': 'application/json'}
        body = json.dumps({"type": "meta"})
        conn.request("POST", "/info", body=body, headers=headers)
        response2 = conn.getresponse()
        tests['post_info_meta'] = {
            'status': response2.status,
            'headers': dict(response2.getheaders()),
            'body': response2.read().decode()[:200]
        }
        
        conn.close()
        
    except Exception as e:
        tests['error'] = str(e)
    
    return jsonify(tests)

@app.route('/check-hyperliquid-docs', methods=['GET'])
def check_hyperliquid_docs():
    """Check what the actual Hyperliquid API expects"""
    # Let's try to find working examples from their docs or GitHub
    tests = {}
    
    # Try the exact format from potential working examples
    test_formats = [
        # Format 1: Simple meta request
        {"type": "meta"},
        
        # Format 2: Maybe they expect different structure
        {"method": "meta"},
        
        # Format 3: Try with action field like exchange endpoints
        {"action": {"type": "meta"}},
        
        # Format 4: Empty request
        {},
    ]
    
    for i, test_format in enumerate(test_formats):
        try:
            response = requests.post(
                "https://api.hyperliquid.xyz/info",
                json=test_format,
                timeout=10
            )
            tests[f'format_{i}'] = {
                'payload': test_format,
                'status': response.status_code,
                'response': response.text[:200]
            }
        except Exception as e:
            tests[f'format_{i}'] = {
                'payload': test_format,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/test-exchange-endpoint', methods=['GET'])
def test_exchange_endpoint():
    """Test the exchange endpoint with different formats"""
    tests = {}
    
    # Different order formats to try
    order_formats = [
        # Format 1: Current format
        {
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
        
        # Format 2: Without grouping
        {
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
        
        # Format 3: Different order structure
        {
            "action": "order",
            "orders": [
                {
                    "coin": "BTC",
                    "side": "A",
                    "sz": "0.001",
                    "order_type": "market"
                }
            ]
        },
        
        # Format 4: Minimal format
        {
            "order": {
                "coin": "BTC",
                "side": "A",
                "sz": "0.001",
                "order_type": "market"
            }
        }
    ]
    
    for i, order_format in enumerate(order_formats):
        try:
            # Sign the request
            message = json.dumps(order_format, separators=(',', ':'), sort_keys=True)
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
                json=order_format,
                headers=headers,
                timeout=10
            )
            
            tests[f'format_{i}'] = {
                'payload': order_format,
                'status': response.status_code,
                'response': response.text,
                'signature': signature[:20] + '...'
            }
            
        except Exception as e:
            tests[f'format_{i}'] = {
                'payload': order_format,
                'error': str(e)
            }
    
    return jsonify(tests)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "message": "Hyperliquid API Discovery",
        "endpoints": {
            "health": "/health",
            "discover_api": "/discover-api",
            "test_raw_http": "/test-raw-http", 
            "check_docs": "/check-hyperliquid-docs",
            "test_exchange": "/test-exchange-endpoint"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

@app.route('/find-working-examples', methods=['GET'])
def find_working_examples():
    """Try to find working API examples"""
    # Common working API patterns from other exchanges
    tests = {}
    
    # Pattern 1: RESTful style
    try:
        response = requests.get("https://api.hyperliquid.xyz/api/v1/info")
        tests['restful_get'] = {
            'status': response.status_code,
            'response': response.text[:200]
        }
    except Exception as e:
        tests['restful_get'] = {'error': str(e)}
    
    # Pattern 2: GraphQL style (some exchanges use this)
    try:
        graphql_query = {
            "query": "query { meta { universe { name } } }"
        }
        response = requests.post(
            "https://api.hyperliquid.xyz/graphql",
            json=graphql_query,
            timeout=10
        )
        tests['graphql'] = {
            'status': response.status_code,
            'response': response.text[:200]
        }
    except Exception as e:
        tests['graphql'] = {'error': str(e)}
    
    return jsonify(tests)
