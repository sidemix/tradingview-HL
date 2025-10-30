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
        
    def order(self, coin, is_buy, sz, order_type="market", limit_price=0):
        """
        Place an order on Hyperliquid using correct API format
        """
        # Hyperliquid expects specific format
        order_payload = {
            "action": {
                "type": "order",
                "orders": [
                    {
                        "a": coin,  # asset
                        "b": is_buy,  # is buy
                        "p": str(limit_price),  # price
                        "s": str(sz),  # size
                        "r": True,  # reduce only
                        "t": {"limit": {"tif": "Gtc"}} if order_type == "limit" else {"market": {}}
                    }
                ]
            }
        }
        
        print(f"Sending order to Hyperliquid: {json.dumps(order_payload, indent=2)}")
        
        # Generate signature
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
            print(f"Hyperliquid API response status: {response.status_code}")
            print(f"Hyperliquid API response text: {response.text}")
            
            if response.status_code == 200:
                try:
                    return response.json()
                except:
                    return {"status": "ok", "response": response.text}
            else:
                return {"status": "error", "error": f"HTTP {response.status_code}: {response.text}"}
                
        except requests.exceptions.RequestException as e:
            print(f"Request error: {str(e)}")
            return {"status": "error", "error": f"Request failed: {str(e)}"}
    
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

    def get_user_state(self):
        """Get user state to verify API connection"""
        info_payload = {
            "type": "userState",
            "user": self.wallet_address
        }
        
        signature = self._sign_request(info_payload)
        headers = {
            "Content-Type": "application/json", 
            "X-API-Signature": signature
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/info",
                json=info_payload,
                headers=headers,
                timeout=10
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}
