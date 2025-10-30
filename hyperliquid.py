from hyperliquid import Hyperliquid
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account
import os
from config import SECRET_KEY, WALLET_ADDRESS

class HyperliquidBot:
    def __init__(self, wallet_address, secret_key, is_testnet=False):
        self.wallet_address = wallet_address
        self.secret_key = secret_key
        self.is_testnet = is_testnet
        
        # Initialize the official Hyperliquid client
        self.client = Hyperliquid(wallet_address, secret_key, 
                                constants.MAINNET_API_URL if not is_testnet else constants.TESTNET_API_URL)
        self.info = Info(constants.MAINNET_API_URL if not is_testnet else constants.TESTNET_API_URL)
        
    def order(self, coin, is_buy, sz, order_type="market", limit_px=0):
        """
        Place an order using the official Hyperliquid SDK
        """
        try:
            print(f"Placing order: {coin}, {is_buy}, {sz}, {order_type}")
            
            # Get asset index (required by Hyperliquid)
            meta = self.info.meta()
            asset_index = None
            
            for i, asset in enumerate(meta['universe']):
                if asset['name'] == coin:
                    asset_index = i
                    break
            
            if asset_index is None:
                return {"status": "error", "error": f"Asset {coin} not found"}
            
            print(f"Found asset index: {asset_index} for {coin}")
            
            # Prepare order parameters (using correct format from docs)
            if order_type == "market":
                order_result = self.client.order(coin, is_buy, sz, None, order_type={"market": {}})
            else:
                order_result = self.client.order(coin, is_buy, sz, limit_px, order_type={"limit": {"tif": "Gtc"}})
            
            print(f"Order result: {order_result}")
            
            if order_result["status"] == "ok":
                return {"status": "success", "response": order_result}
            else:
                return {"status": "error", "error": order_result.get("response", "Unknown error")}
                
        except Exception as e:
            print(f"Order error: {str(e)}")
            return {"status": "error", "error": str(e)}
    
    def get_user_state(self):
        """Get user state"""
        try:
            user_state = self.info.user_state(self.wallet_address)
            return user_state
        except Exception as e:
            return {"error": str(e)}
    
    def get_meta(self):
        """Get exchange metadata"""
        try:
            return self.info.meta()
        except Exception as e:
            return {"error": str(e)}
