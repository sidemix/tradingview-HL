import os

# Hyperliquid configuration
WALLET_ADDRESS = os.environ.get('WALLET_ADDRESS')
SECRET_KEY = os.environ.get('SECRET_KEY')

# TradingView webhook secret (optional)
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')
