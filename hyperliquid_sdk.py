from hyperliquid import Hyperliquid as OfficialHL
from hyperliquid.info import Info
from hyperliquid.utils import constants
import os
from config import SECRET_KEY, WALLET_ADDRESS

class HyperliquidSDK:
    def __init__(self, wallet_address, secret_key):
        self.wallet_address = wallet_address
        self.secret_key = secret_key
        self.client = OfficialHL(wallet_address, secret_key, constants.MAINNET_API_URL)
        
    def order(self, coin, is_buy, sz, order_type="market", limit_px=0):
        try:
            print(f"Placing order via SDK: {coin}, {is_buy}, {sz}, {order_type}")
            
            if order_type == "market":
                result = self.client.order(coin, is_buy, sz, None, order_type={"market": {}})
            else:
                result = self.client.order(coin, is_buy, sz, limit_px, order_type={"limit": {"tif": "Gtc"}})
            
            print(f"SDK Order result: {result}")
            
            if result["status"] == "ok":
                return {"status": "success", "response": result}
            else:
                return {"status": "error", "error": result.get("response", "Unknown error")}
                
        except Exception as e:
            print(f"SDK Order error: {str(e)}")
            return {"status": "error", "error": str(e)}
