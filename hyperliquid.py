import requests
import time
import hmac
import hashlib
import json

class Hyperliquid:
    def __init__(self, wallet_address, secret_key, base_url="https://api.hyperliquid.xyz"):
        self.wallet_address = wallet_address
        self.secret_key = secret_key
        self.base_url = base_url
        
    def order(self, coin, is_buy, sz, order_type="market", limit_price=0):
        """
        Place an order on Hyperliquid
        """
        endpoint = "/exchange"
        
        # Prepare order data
        order_data = {
            "action": {
                "type": "order",
                "coin": coin,
                "is_buy": is_buy,
                "sz": str(sz),
                "limit_px": str(limit_price),
                "order_type": {"limit": {"tif": "Gtc"}} if order_type == "limit" else {"market": {}}
            }
        }
        
        print(f"Sending order to Hyperliquid: {order_data}")
        
        # Generate signature
        signature = self._sign_request(order_data)
        
        headers = {
            "Content-Type": "application/json",
            "X-API-Signature": signature
        }
        
        try:
            response = requests.post(
                f"{self.base_url}{endpoint}",
                json=order_data,
                headers=headers,
                timeout=10  # Add timeout
            )
            print(f"Hyperliquid API response status: {response.status_code}")
            print(f"Hyperliquid API response text: {response.text}")
            
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Request error: {str(e)}")
            return {"status": "error", "error": f"Request failed: {str(e)}"}
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {str(e)}")
            return {"status": "error", "error": f"Invalid response from Hyperliquid: {str(e)}"}
    
    def _sign_request(self, data):
        """
        Sign the request using HMAC-SHA256
        """
        message = json.dumps(data, separators=(',', ':'), sort_keys=True)
        signature = hmac.new(
            bytes(self.secret_key, 'utf-8'),
            msg=bytes(message, 'utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        return signature
