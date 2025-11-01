from flask import Flask, request, jsonify
import logging, os, json, time
from dotenv import load_dotenv
import inspect
import requests

# Hyperliquid SDK
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Optional: show SDK version in logs
try:
    from importlib.metadata import version as _pkg_version
    HL_SDK_VERSION = _pkg_version("hyperliquid-python-sdk")
except Exception:
    HL_SDK_VERSION = "unknown"

# Defensive imports (SDK structure varies)
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

# Flask setup
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
        self.info_url = f"{self.base_url}/info"
        self.exchange_url = f"{self.base_url}/exchange"

        logger.info(f"HL SDK version: {HL_SDK_VERSION}")
        logger.info(f"HL base_url:    {self.base_url}")
        logger.info(f"HL address:     {self.account_address}")
        logger.info(f"Network:        {'testnet' if self.use_testnet else 'mainnet'}")

        self.info = Info(self.base_url, skip_ws=True)

        if not self.account_address or not self.secret_key:
            logger.warning("Hyperliquid credentials not set — running in DEMO mode")
            self.exchange = None
            self.initialized = False
            return

        # Try to init SDK exchange (if any)
        self.exchange = None
        if Exchange:
            self.exchange = self._init_exchange_dynamic()

        # Fall back to HTTP signing if needed
        if not self.exchange and not (Wallet or sign_l1_action):
            logger.error("❌ No working Exchange constructor or signer available — DEMO mode.")
            self.initialized = False
            return

        # Verify balance to confirm connection
        try:
            st = self.info.user_state(self.account_address.lower())
            balance = float(st.get("withdrawable", 0)) if st else 0.0
            logger.info(f"✅ Hyperliquid ready! Balance: {balance} (mode: {'SDK' if self.exchange else 'HTTP-signed'})")
            self.initialized = True
        except Exception as e:
            logger.warning(f"Ready, but failed to read balance: {e}")
            self.initialized = True

    # --- dynamic Exchange constructor discovery ---
    def _init_exchange_dynamic(self):
        sig = inspect.signature(Exchange.__init__)
        params = [p.name for p in sig.parameters.values()]
        param_set = set(params)

        addr_keys = ["account_address", "address", "account", "addr"]
        priv_keys = ["priv_key", "private_key", "secret_key", "secret", "key"]
        base_keys = ["base_url", "url"]

        addr_key = next((k for k in addr_keys if k in param_set), None)
        priv_key = next((k for k in priv_keys if k in param_set), None)
        base_key = next((k for k in base_keys if k in param_set), None)

        if addr_key and priv_key and base_key:
            try:
                ex = Exchange(**{
                    addr_key: self.account_address,
                    priv_key: self.secret_key,
                    base_key: self.base_url
                })
                logger.info("✅ Exchange init succeeded via keyword mapping")
                return ex
            except Exception as e:
                logger.warning(f"Keyword mapping failed: {e}")

        attempts = []
        def try_ctor(args, label):
            try:
                ex = Exchange(*args)
                logger.info(f"✅ Exchange init succeeded via positional ctor: {label}")
                return ex
            except Exception as e:
                logger.warning(f"Exchange ctor {label} failed: {e}")
                attempts.append((label, str(e)))
                return None

        for label, args in [
            ("[base, addr, priv]", (self.base_url, self.account_address, self.secret_key)),
            ("[addr, priv, base]", (self.account_address, self.secret_key, self.base_url)),
            ("[addr, priv]", (self.account_address, self.secret_key)),
        ]:
            ex = try_ctor(args, label)
            if ex:
                return ex

        logger.error("❌ All Exchange constructor attempts failed:")
        for lbl, err in attempts:
            logger.error(f"   - {lbl}: {err}")
        return None

    @staticmethod
    def _normalize_symbol(sym: str) -> str:
        s = (sym or "BTC").upper().strip()
        return s.replace("/USD", "").replace("-PERP", "")

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0

    # --- MARKET ORDER LOGIC ---
    def market_order(self, coin: str, is_buy: bool, sz: float, slippage: float = 0.01):
        """Try SDK .market_open; else IOC; else raw signed HTTP."""
        coin = self._normalize_symbol(coin)

        # Try SDK path
        if self.exchange:
            if hasattr(self.exchange, "market_open"):
                try:
                    logger.info(f"Submitting market_open: {coin} {'BUY' if is_buy else 'SELL'} {sz}")
                    return self.exchange.market_open(coin, is_buy, sz, None, slippage)
                except Exception as e:
                    logger.warning(f"market_open failed, fallback: {e}")

            order_req = {
                "coin": coin,
                "is_buy": is_buy,
                "sz": str(sz),
                "limit_px": "0",
                "reduce_only": False,
                "order_type": {"limit": {"tif": "Ioc"}},
            }
            try:
                return self.exchange.order(order_req, grouping="na")
            except Exception as e:
                logger.warning(f"SDK order() failed: {e} — trying HTTP-signed")

        # HTTP-signed fallback
        if not (Wallet or sign_l1_action):
            return {"status": "error", "message": "No signing available"}

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

        # ✅ FIX: add missing args for your SDK version
        expires_after_ms = 45_000
        is_mainnet = not self.use_testnet

        try:
            if Wallet is not None:
                w = Wallet(self.account_address, self.secret_key)
                sig = w.sign_l1_action(action, nonce, expires_after_ms, is_mainnet)
            else:
                sig = sign_l1_action(
                    self.account_address,
                    self.secret_key,
                    action,
                    nonce,
                    expires_after_ms,
                    is_mainnet
                )

            body = {"action": action, "nonce": nonce, "signature": sig}
            logger.info("Submitting IOC market-equivalent (HTTP-signed)")
            r = requests.post(self.exchange_url, json=body, timeout=10)
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return {"status": "error", "message": f"{r.status_code} {r.text}"}
        except Exception as e:
            logger.exception("HTTP-signed order failed")
            return {"status": "error", "message": str(e)}

    def _asset_index(self, coin: str) -> int:
        meta = self.info.meta()
        for i, a in enumerate(meta.get("universe", [])):
            if a.get("name", "").upper() == coin.upper():
                return i
        raise ValueError(f"Asset not found: {coin}")


# --- APP ROUTES ---

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
                "message": f"[DEMO] {trader._normalize_symbol(symbol)} {'BUY' if is_buy else 'SELL'} {qty}",
                "note": "Hyperliquid not initialized"
            }), 200

        result = trader.market_order(symbol, is_buy, qty)

        if isinstance(result, dict) and result.get("status") == "ok":
            return jsonify({
                "status": "success",
                "message": f"Trade executed: {trader._normalize_symbol(symbol)} {'BUY' if is_buy else 'SELL'} {qty}",
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
        "message": "TradingView ➜ Hyperliquid Webhook Server (SDK/HTTP auto)",
        "endpoints": {"health": "/health (GET)", "webhook": "/webhook/tradingview (POST)"},
        "status": "ACTIVE"
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
