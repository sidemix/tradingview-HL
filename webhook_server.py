from flask import Flask, request, jsonify
import logging, os, json, time, requests
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

# module signer
try:
    from hyperliquid.utils.signing import sign_l1_action
except Exception:
    sign_l1_action = None

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("webhook_server")


class HyperliquidTrader:
    def __init__(self):
        self.use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"
        self.account_address = (os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
        self.priv_key = (os.getenv("HYPERLIQUID_SECRET_KEY") or "").strip()

        # Keep priv as-is (most wheels accept 0x-prefixed hex)
        self.base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        self.exchange_url = f"{self.base_url}/exchange"
        self.info = Info(self.base_url, skip_ws=True)

        logger.info(f"HL base_url: {self.base_url}")
        logger.info(f"HL address:  {self.account_address}")
        logger.info(f"Network:     {'testnet' if self.use_testnet else 'mainnet'}")

        if not (self.account_address and self.priv_key and sign_l1_action):
            logger.warning("⚠️ Missing address/secret or signer — DEMO mode")
            self.initialized = False
            return

        try:
            st = self.info.user_state(self.account_address.lower())
            bal = float(st.get("withdrawable", 0)) if st else 0.0
            logger.info(f"✅ Connected to HL (HTTP-signed). Balance: {bal}")
        except Exception as e:
            logger.warning(f"Connected, balance check failed: {e}")
        self.initialized = True

    @staticmethod
    def _normalize_symbol(sym: str) -> str:
        return (sym or "BTC").upper().replace("/USD", "").replace("-PERP", "")

    def _asset_index(self, coin: str) -> int:
        meta = self.info.meta()
        for i, a in enumerate(meta.get("universe", [])):
            if a.get("name", "").upper() == coin.upper():
                return i
        raise ValueError(f"Asset not found: {coin}")

    def _sign(self, action: dict, nonce: int):
        """
        Your wheel’s logs showed:
          - missing 'is_mainnet' -> wants the 6-pos form
          - active_pool variants caused 'to_bytes' / 'startswith' errors
        So we try, in order:
          (addr, priv, action, nonce, expires, is_mainnet)   # ✅ primary
          (addr, priv, action, nonce, expires)               # fallback (older)
          (addr, priv, action, 1, nonce, expires)            # rare pool=int
        """
        expires_after_ms = 45_000
        is_mainnet = not self.use_testnet
        addr = self.account_address
        priv = self.priv_key

        last_err = None
        variants = [
            (addr, priv, action, nonce, expires_after_ms, is_mainnet),
            (addr, priv, action, nonce, expires_after_ms),
            (addr, priv, action, 1, nonce, expires_after_ms),
        ]

        for i, args in enumerate(variants, 1):
            try:
                sig = sign_l1_action(*args)
                logger.info(f"✅ sign_l1_action positional v#{i} OK (argc={len(args)})")
                return sig
            except TypeError as te:
                logger.warning(f"sign_l1_action positional v#{i} TypeError: {te}")
                last_err = te
            except Exception as e:
                logger.warning(f"sign_l1_action positional v#{i} failed: {e}")
                last_err = e

        raise RuntimeError(f"No compatible signer signature found ({last_err})")

    def market_order(self, coin: str, is_buy: bool, sz: float):
        coin = self._normalize_symbol(coin)

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
            sig = self._sign(action, nonce)
            body = {"action": action, "nonce": nonce, "signature": sig}
            logger.info(f"Submitting order: {coin} {'BUY' if is_buy else 'SELL'} {sz}")
            r = requests.post(self.exchange_url, json=body, timeout=12)
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return {"status": "error", "message": f"{r.status_code} {r.text}"}
        except Exception as e:
            logger.exception("Signing or HTTP submit failed")
            return {"status": "error", "message": str(e)}

    def get_balance(self) -> float:
        try:
            st = self.info.user_state(self.account_address.lower())
            return float(st.get("withdrawable", 0)) if st else 0.0
        except Exception:
            return 0.0


trader = HyperliquidTrader()


@app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    try:
        data = request.get_json(silent=True) or json.loads(request.data.decode("utf-8"))
        logger.info(f"Received TradingView alert: {data}")

        symbol = str(data.get("symbol", "BTC"))
        action = str(data.get("action", "buy")).lower()
        qty = float(data.get("quantity", 0.01))
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
        "credentials_set": bool(trader.account_address and trader.priv_key),
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


