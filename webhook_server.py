from flask import Flask, request, jsonify
import logging, os, json, time, inspect, requests
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Optional: show SDK version
try:
    from importlib.metadata import version as _pkg_version
    HL_SDK_VERSION = _pkg_version("hyperliquid-python-sdk")
except Exception:
    HL_SDK_VERSION = "unknown"

# Defensive imports
Exchange = None
Wallet = None
sign_l1_action = None
try:
    from hyperliquid.exchange import Exchange as _Exchange
    Exchange = _Exchange
except Exception:
    pass
try:
    from hyperliquid.utils.wallet import Wallet as _Wallet
    Wallet = _Wallet
except Exception:
    pass
try:
    from hyperliquid.utils.signing import sign_l1_action as _sign_l1_action
    sign_l1_action = _sign_l1_action
except Exception:
    pass

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("webhook_server")


class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = (os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
        self.secret_key = (os.getenv("HYPERLIQUID_SECRET_KEY") or "").strip()
        self.base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        self.exchange_url = f"{self.base_url}/exchange"

        logger.info(f"HL SDK version: {HL_SDK_VERSION}")
        logger.info(f"HL base_url:    {self.base_url}")
        logger.info(f"HL address:     {self.account_address}")
        logger.info(f"Network:        {'testnet' if self.use_testnet else 'mainnet'}")

        self.info = Info(self.base_url, skip_ws=True)

        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set — DEMO mode")
            self.exchange = None
            self.initialized = False
            return

        # Best-effort Exchange init (not required; we’ll use HTTP-signed path)
        self.exchange = None
        if Exchange:
            try:
                # Some SDKs accept this order; if not, it will be ignored (we're using HTTP path anyway)
                self.exchange = Exchange(self.base_url, self.account_address, self.secret_key)
            except Exception:
                self.exchange = None

        # Verify connection
        st = self.info.user_state(self.account_address.lower())
        balance = float(st.get("withdrawable", 0)) if st else 0.0
        logger.info(f"✅ Hyperliquid ready! Balance: {balance} (mode: {'SDK' if self.exchange else 'HTTP-signed'})")
        self.initialized = True

    @staticmethod
    def _normalize_symbol(sym: str) -> str:
        return (sym or "BTC").upper().replace("/USD", "").replace("-PERP", "")

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0

    def _asset_index(self, coin: str) -> int:
        meta = self.info.meta()
        for i, a in enumerate(meta.get("universe", [])):
            if a.get("name", "").upper() == coin.upper():
                return i
        raise ValueError(f"Asset not found: {coin}")

    def _sign_action(self, action: dict, nonce: int):
        """
        Robust signer that supports multiple SDK signatures:
        1) Wallet.sign_l1_action(action, nonce, expires_after_ms, is_mainnet)
        2) sign_l1_action(address, priv, action, nonce, expires_after_ms, is_mainnet)
        3) sign_l1_action(address, priv, action, 'perp', nonce, expires_after_ms)
        4) sign_l1_action(address, priv, action, 'perp', nonce, expires_after_ms, is_mainnet)
        """
        expires_after_ms = 45_000
        is_mainnet = not self.use_testnet
        addr = self.account_address
        priv = self.secret_key

        # Variant 1: Wallet signer
        if Wallet:
            try:
                w = Wallet(addr, priv)
                return w.sign_l1_action(action, nonce, expires_after_ms, is_mainnet)
            except TypeError:
                pass
            except Exception as e:
                logger.warning(f"Wallet signer failed: {e}")

        # Variant 2: Module signer (common)
        if sign_l1_action:
            # Try (addr, priv, action, nonce, expires, is_mainnet)
            try:
                return sign_l1_action(addr, priv, action, nonce, expires_after_ms, is_mainnet)
            except TypeError:
                pass
            except Exception as e:
                logger.warning(f"sign_l1_action v2 failed: {e}")

            # Try (addr, priv, action, 'perp', nonce, expires)
            try:
                return sign_l1_action(addr, priv, action, "perp", nonce, expires_after_ms)
            except TypeError:
                pass
            except Exception as e:
                logger.warning(f"sign_l1_action v3 failed: {e}")

            # Try (addr, priv, action, 'perp', nonce, expires, is_mainnet)
            try:
                return sign_l1_action(addr, priv, action, "perp", nonce, expires_after_ms, is_mainnet)
            except Exception as e:
                logger.warning(f"sign_l1_action v4 failed: {e}")

        raise RuntimeError("No compatible signer signature found")

    def market_order(self, coin: str, is_buy: bool, sz: float):
        coin = self._normalize_symbol(coin)

        # Build market-equivalent action (IOC)
        action = {
            "type": "order",
            "orders": [{
                "a": self._asset_index(coin),
                "b": bool(is_buy),
                "p": "0",
                "s": str(sz),
                "r": False,
                "t": {"limit": {"tif": "Ioc"}}
            }],
            "grouping": "na"
        }
        nonce = int(time.time() * 1000)

        try:
            sig = self._sign_action(action, nonce)
            body = {"action": action, "nonce": nonce, "signature": sig}
            logger.info(f"Submitting market order (HTTP-signed): {coin} {'BUY' if is_buy else 'SELL'} {sz}")
            r = requests.post(self.exchange_url, json=body, timeout=12)

            # Try JSON; otherwise return text error
            try:
                return r.json()
            except Exception:
                return {"status": "error", "message": f"{r.status_code} {r.text}"}

        except Exception as e:
            logger.exception("Signing or HTTP submit failed")
            return {"status": "error", "message": str(e)}


trader = HyperliquidTrader()


@app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    try:
        data = request.get_json(silent=True) or json.loads(request.data.decode("utf-8"))
        logger.info(f"Received TradingView alert: {data}")

        symbol = str(data.get("symbol", "BTC"))
        action = str(data.get("action", "buy")).lower()
        qty = float(data.get("quantity", 0.001))
        is_buy = action in ("buy", "long")

        if not trader.initialized:
            return jsonify({
                "status": "demo",
                "message": f"[DEMO] {symbol} {'BUY' if is_buy else 'SELL'} {qty}",
                "note": "Hyperliquid not initialized"
            }), 200

        result = trader.market_order(symbol, is_buy, qty)

        if isinstance(result, dict) and result.get("status") == "ok":
            return jsonify({
                "status": "success",
                "message": f"Trade executed: {symbol} {'BUY' if is_buy else 'SELL'} {qty}",
                "result": result
            }), 200

        return jsonify({"status": "error", "message": "Trade failed", "result": result}), 400

    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "trading": "active" if trader.initialized else "demo",
        "balance": trader.get_balance(),
        "credentials_set": bool(trader.account_address and trader.secret_key),
        "network": "testnet" if trader.use_testnet else "mainnet"
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "TradingView ➜ Hyperliquid Webhook Server",
        "status": "ACTIVE"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
