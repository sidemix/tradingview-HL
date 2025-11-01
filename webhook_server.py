from flask import Flask, request, jsonify
import logging, os, json, time, requests
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Try to import signer
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
        self.address = (os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") or "").strip()
        self.priv = (os.getenv("HYPERLIQUID_SECRET_KEY") or "").strip()

        self.base_url = constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL
        self.exchange_url = f"{self.base_url}/exchange"
        self.info = Info(self.base_url, skip_ws=True)

        logger.info(f"HL base_url: {self.base_url}")
        logger.info(f"HL address:  {self.address}")
        logger.info(f"Network:     {'testnet' if self.use_testnet else 'mainnet'}")

        if not (self.address and self.priv and sign_l1_action):
            logger.warning("⚠️ Missing address/secret or signer — DEMO mode")
            self.initialized = False
            self.sign_variant = None
            return

        # Probe signer at boot with a harmless dummy action
        self.sign_variant = self._detect_signer_variant()

        try:
            st = self.info.user_state(self.address.lower())
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

    def _detect_signer_variant(self):
        """
        Try a comprehensive matrix of positional signatures and cache the one that works.
        We sign a no-op-ish dummy action locally (no HTTP call), so it's safe.
        """
        if not sign_l1_action:
            return None

        is_mainnet = not self.use_testnet
        nonce = int(time.time() * 1000)
        expires = 45000

        # dummy minimal action (structure only; not submitted)
        dummy_action = {"type": "heartbeat"}  # simplest possible thing to hash

        addr = self.address
        acct = {"address": self.address}      # some wheels expect this first

        # Build a matrix of positional tuples to try (in order)
        candidates = [
            # Common 6-pos: addr, priv, action, nonce, expires, is_mainnet
            (addr, self.priv, dummy_action, nonce, expires, is_mainnet),
            # Same, with account dict first (some wheels expect account first)
            (acct, self.priv, dummy_action, nonce, expires, is_mainnet),

            # With active_pool inserted as 4th arg (perp=1)
            (addr, self.priv, dummy_action, 1, nonce, expires, is_mainnet),
            (acct, self.priv, dummy_action, 1, nonce, expires, is_mainnet),

            # With active_pool omitted, no is_mainnet (older wheels)
            (addr, self.priv, dummy_action, nonce, expires),
            (acct, self.priv, dummy_action, nonce, expires),

            # With active_pool=0 (spot), still include is_mainnet
            (addr, self.priv, dummy_action, 0, nonce, expires, is_mainnet),
            (acct, self.priv, dummy_action, 0, nonce, expires, is_mainnet),

            # With active_pool as bytes b"perp"
            (addr, self.priv, dummy_action, b"perp", nonce, expires, is_mainnet),
            (acct, self.priv, dummy_action, b"perp", nonce, expires, is_mainnet),
        ]

        last_err = None
        for i, args in enumerate(candidates, 1):
            try:
                sign_l1_action(*args)  # we only test signing; no submission
                logger.info(f"✅ Detected signer positional variant #{i} (argc={len(args)})")
                return args  # return the ARG SHAPE that worked
            except TypeError as te:
                logger.warning(f"sign_l1_action probe v#{i} TypeError: {te}")
                last_err = te
            except Exception as e:
                logger.warning(f"sign_l1_action probe v#{i} failed: {e}")
                last_err = e

        logger.error(f"❌ No compatible signer signature found during probe ({last_err})")
        return None

    def _sign(self, action: dict, nonce: int):
        if not self.sign_variant:
            # last-ditch: re-probe (in case wheel changed between builds)
            self.sign_variant = self._detect_signer_variant()
            if not self.sign_variant:
                raise RuntimeError("No compatible signer signature found")

        # The detected variant is just the tuple SHAPE we should use.
        # Replace the dummy_action, nonce, expires, etc. with live values.
        is_mainnet = not self.use_testnet
        expires = 45000
        addr = self.address
        acct = {"address": self.address}

        variant = self.sign_variant
        argc = len(variant)

        # Map shape dynamically
        if argc == 6:
            # Could be (addr/acct, priv, action, nonce, expires, is_mainnet)
            base0, base1, *_ = variant[:2]
            first = addr if isinstance(base0, str) else acct
            args = (first, self.priv, action, nonce, expires, is_mainnet)
        elif argc == 7:
            # Could be (addr/acct, priv, action, active_pool, nonce, expires, is_mainnet)
            base0, base1, *_ = variant[:2]
            first = addr if isinstance(base0, str) else acct
            active_pool = variant[3]
            args = (first, self.priv, action, active_pool, nonce, expires, is_mainnet)
        elif argc == 5:
            # (addr/acct, priv, action, nonce, expires)
            base0, base1, *_ = variant[:2]
            first = addr if isinstance(base0, str) else acct
            args = (first, self.priv, action, nonce, expires)
        else:
            # Try to be safe: rebuild based on types we saw in probe
            base0 = variant[0]
            first = addr if isinstance(base0, str) else acct
            args = (first, self.priv, action, nonce, expires, is_mainnet)

        return sign_l1_action(*args)

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
            st = self.info.user_state(self.address.lower())
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
        "credentials_set": bool(trader.address and trader.priv),
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
