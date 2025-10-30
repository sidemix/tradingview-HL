import requests
import hmac
import hashlib
import json
import time

class Hyperliquid:
    def __init__(self, wallet_address, secret_key, base_url="https://api.hyperliquid.xyz"):
        self.wallet_address = wallet_address
        self.secret_key = secret_key
        self.base_url = base_url
        
    def order(self, coin, is_buy, sz, order_type="market", limit_px=0):
        """
        Place an order using Hyperliquid API
        """
        # Create order based on type
        if order_type == "market":
            order = {
                "coin": coin,
                "side": "A" if is_buy else "B",
                "sz": str(sz),
                "order_type": {"market": {}}
            }
        else:
            order = {
                "coin": coin,
                "side": "A" if is_buy else "B", 
                "sz": str(sz),
                "limit_px": str(limit_px),
                "order_type": {"limit": {"tif": "Gtc"}}
            }
        
        order_payload = {
            "action": {
                "type": "order",
                "orders": [order],
                "grouping": "na"
            }
        }
        
        print(f"Sending order: {json.dumps(order_payload, indent=2)}")
        
        # Sign the request
        signature = self._sign_request(order_payload)
        
        headers = {
            "Content-Type": "application/json",
            "X-API-Signature": signature
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/exchange",
                json=order_payload,
                headers=headers,
                timeout=10
            )
            
            print(f"Response status: {response.status_code}")
            print(f"Response text: {response.text}")
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    if response_data.get("status") == "ok":
                        return {"status": "success", "response": response_data}
                    else:
                        return {"status": "error", "error": response_data.get("response", response_data)}
                except Exception as e:
                    return {"status": "error", "error": f"JSON parse error: {str(e)}"}
            else:
                return {"status": "error", "error": f"HTTP {response.status_code}: {response.text}"}
                
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    def _sign_request(self, data):
        """
        Sign the request using HMAC-SHA256
        """
        message = json.dumps(data, separators=(',', ':'), sort_keys=True)
        print(f"Signing message: {message}")
        signature = hmac.new(
            bytes(self.secret_key, 'utf-8'),
            msg=bytes(message, 'utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        return signature

    def get_exchange_info(self):
        """Get exchange meta information"""
        info_payload = {
            "type": "meta"
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/info",
                json=info_payload,
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}: {response.text}"}
        except Exception as e:
            return {"error": str(e)}

    def get_user_state(self):
        """Get user state to verify connection"""
        info_payload = {
            "type": "userState",
            "user": self.wallet_address
        }
        
        # Don't sign user state requests - they are public info endpoints
        try:
            response = requests.post(
                f"{self.base_url}/info",
                json=info_payload,
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}: {response.text}"}
        except Exception as e:
            return {"error": str(e)}

    def get_available_coins(self):
        """Get list of available trading coins"""
        info = self.get_exchange_info()
        if isinstance(info, dict) and 'universe' in info:
            coins = [item['name'] for item in info['universe']]
            return coins
        return []
