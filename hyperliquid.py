import requests
import hmac
import hashlib
import json
import time

class HyperliquidDirect:
    def __init__(self, wallet_address, secret_key, base_url="https://api.hyperliquid.xyz"):
        self.wallet_address = wallet_address
        self.secret_key = secret_key
        self.base_url = base_url
        
    def order(self, coin, is_buy, sz, order_type="market", limit_px=0):
        """
        Place order using the CORRECT API format from official documentation
        """
        try:
            # First get asset index
            meta_response = requests.post(f"{self.base_url}/info", json={"type": "meta"})
            if meta_response.status_code != 200:
                return {"status": "error", "error": f"Failed to get meta: {meta_response.text}"}
                
            meta = meta_response.json()
            asset_index = None
            
            for i, asset in enumerate(meta['universe']):
                if asset['name'] == coin:
                    asset_index = i
                    break
            
            if asset_index is None:
                return {"status": "error", "error": f"Asset {coin} not found"}
            
            print(f"Found asset index: {asset_index} for {coin}")
            
            # Create base order without price for market orders
            order_data = {
                "a": asset_index,        # asset index
                "b": is_buy,             # is_buy (boolean)
                "s": str(sz),            # size (string)
                "r": False,              # reduce_only
            }
            
            # Add order type specific fields
            if order_type == "market":
                order_data["t"] = {"market": {}}  # No price for market orders
            else:
                order_data["t"] = {"limit": {"tif": "Gtc"}}
                order_data["p"] = str(limit_px)   # Only add price for limit orders
            
            # Create the complete payload
            order_payload = {
                "action": {
                    "type": "order",
                    "orders": [order_data],
                    "grouping": "na"
                },
                "nonce": int(time.time() * 1000)  # REQUIRED: current timestamp in milliseconds
            }
            
            print(f"Sending order payload: {json.dumps(order_payload, indent=2)}")
            
            # Sign the COMPLETE payload (including nonce)
            signature = self._sign_request(order_payload)
            
            headers = {
                "Content-Type": "application/json",
                "X-API-Signature": signature
            }
            
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
                        return {"status": "error", "error": response_data}
                except Exception as e:
                    return {"status": "error", "error": f"Invalid JSON: {response.text}"}
            else:
                return {"status": "error", "error": f"HTTP {response.status_code}: {response.text}"}
                
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    def _sign_request(self, data):
        """
        Sign the COMPLETE request using HMAC-SHA256
        """
        message = json.dumps(data, separators=(',', ':'), sort_keys=True)
        print(f"Signing message: {message}")
        signature = hmac.new(
            bytes(self.secret_key, 'utf-8'),
            msg=bytes(message, 'utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        print(f"Generated signature: {signature}")
        return signature

    def get_user_state(self):
        """Get user state"""
        try:
            response = requests.post(
                f"{self.base_url}/info",
                json={"type": "clearinghouseState", "user": self.wallet_address},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}: {response.text}"}
        except Exception as e:
            return {"error": str(e)}

    def get_meta(self):
        """Get exchange metadata"""
        try:
            response = requests.post(
                f"{self.base_url}/info",
                json={"type": "meta"},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}: {response.text}"}
        except Exception as e:
            return {"error": str(e)}
