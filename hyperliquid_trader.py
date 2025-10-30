import os
import json
import logging
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
        self.secret_key = os.getenv("HYPERLIQUID_SECRET_KEY")
        
        if not self.account_address or not self.secret_key:
            raise ValueError("Missing required environment variables")
        
        # Initialize Hyperliquid clients
        self.info = Info(constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(
            self.account_address, 
            self.secret_key, 
            base_url=constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        )
        
        logger.info(f"Initialized Hyperliquid trader for {self.account_address} on {'testnet' if self.use_testnet else 'mainnet'}")

    def get_asset_index(self, coin: str) -> int:
        """Get asset index from coin symbol"""
        meta = self.info.meta()
        for i, asset in enumerate(meta["universe"]):
            if asset["name"] == coin.upper():
                return i
        raise ValueError(f"Coin {coin} not found in universe")

    def place_market_order(self, coin: str, is_buy: bool, size: float) -> dict:
        """Place market order"""
        asset_index = self.get_asset_index(coin)
        
        order_result = self.exchange.order(
            coin, 
            is_buy, 
            size, 
            limit_px=0,  # 0 for market order
            order_type={"limit": {"tif": "Gtc"}}
        )
        
        logger.info(f"Market order placed: {coin} {'BUY' if is_buy else 'SELL'} {size}")
        return order_result

    def place_limit_order(self, coin: str, is_buy: bool, size: float, price: float) -> dict:
        """Place limit order"""
        asset_index = self.get_asset_index(coin)
        
        order_result = self.exchange.order(
            coin,
            is_buy,
            size,
            limit_px=price,
            order_type={"limit": {"tif": "Gtc"}}
        )
        
        logger.info(f"Limit order placed: {coin} {'BUY' if is_buy else 'SELL'} {size} @ {price}")
        return order_result

    def get_user_state(self) -> dict:
        """Get user account state"""
        return self.info.user_state(self.account_address)

    def check_balance(self) -> float:
        """Check available balance"""
        user_state = self.get_user_state()
        return float(user_state["withdrawable"])
