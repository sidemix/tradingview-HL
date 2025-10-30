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
        Place an order using Hyperliquid API - CORRECT FORMAT
        """
        # CORRECT order format based on Hyperliquid API documentation
        order_payload = {
            "action": {
                "type": "order",
                "orders": [
                    {
                        "a": coin,  # asset (coin)
                        "b": is_buy,  # is_buy (boolean)
                        "p": float(limit_px),  # price (number)
                        "s": float(sz),  # size (number)
                        "r": False,  # reduce_only (boolean)
                        "t": {"limit": {"tif": "Gtc"}} if order_type == "limit" else {"market": {}}
                    }
                ],
                "grouping": "na"
            }
        }
        
        print(f"Sending order: {json.dumps(order_payload, indent=2)}")
        
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
            print(f"Response headers: {dict(response.headers)}")
            print(f"Response text: {response.text}")
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    return {"status": "success", "response": response_data}
                except:
                    return {"status": "success", "response": response.text}
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
        print(f"Signature: {signature}")
        return signature

    def get_exchange_info(self):
        """Get exchange info to verify symbols"""
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

    def get_available_coins(self):
        """Get list of available coins"""
        info = self.get_exchange_info()
        if 'universe' in info:
            coins = [item['name'] for item in info['universe']]
            return coins
        return []
