# core.py
"""
ExchangeClient موحّد:
- Public data (klines, price) لأي وضع تشغيل.
- Live trading عبر python-binance إذا كان dry_run=False ومفاتيح API موجودة.
- Testnet مدعوم اختياريًا.
- round_qty بناءً على قيود الرمز (LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER).

الاعتمادات: python-binance (موجودة في requirements.txt)
config.yaml (مثال):
binance:
  api_key: "..."
  api_secret: "..."
  testnet: true
trade:
  dry_run: true          # لو False + مفاتيح → ينفّذ أوامر حقيقية
  paper_cash: 10000      # للكاش الورقي عند اللزوم
"""

from __future__ import annotations

import time
import math
from typing import Any, Dict, Optional

import requests

try:
    from binance.client import Client as BinanceClient
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
except Exception:
    BinanceClient = None
    SIDE_BUY = "BUY"
    SIDE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"


class ExchangeClient:
    REST_BASE = "https://api.binance.com"
    REST_BASE_TESTNET = "https://testnet.binance.vision"

    def __init__(self, cfg: dict, reporter: Any):
        self.cfg = cfg
        self.reporter = reporter
        self.session = requests.Session()

        bcfg = cfg.get("binance", {}) or {}
        self.api_key = bcfg.get("api_key")
        self.api_secret = bcfg.get("api_secret")
        self.testnet = bool(bcfg.get("testnet", False))
        self.dry_run = bool(cfg.get("trade", {}).get("dry_run", True))

        self._symbol_filters: Dict[str, Dict[str, float]] = {}  # cache lot/step/min filters

        # Binance SDK client (للتداول الحقيقي فقط)
        self._bnc = None
        if (not self.dry_run) and self.api_key and self.api_secret and BinanceClient is not None:
            self._bnc = BinanceClient(self.api_key, self.api_secret, testnet=self.testnet)
            # اضبط الـbase URL صراحة لو testnet
            if self.testnet:
                self._bnc.API_URL = self.REST_BASE_TESTNET
            self.reporter.log(f"[Exchange] Live trading enabled. testnet={self.testnet}")
        else:
            mode = "DRY-RUN" if self.dry_run else "NO-CLIENT"
            self.reporter.log(f"[Exchange] Live trading disabled ({mode}). Using public endpoints only.")

    # ------------------------ Utilities ------------------------

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        # يدعم "BTCUSDT" و "BTC/USDT"
        return symbol.replace("/", "").upper()

    def _rest_get(self, path: str, params: Optional[dict] = None, timeout: int = 10):
        base = self.REST_BASE_TESTNET if self.testnet else self.REST_BASE
        url = f"{base}{path}"
        r = self.session.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ------------------------ Public Data ------------------------

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 500):
        """
        يرجّع klines بصيغة Binance القياسية:
        [open_time, open, high, low, close, volume, close_time, ...]
        """
        sym = self._normalize_symbol(symbol)
        # لو عندنا SDK نقدر نستخدمها، بس الـREST أسرع وأخف هنا
        return self._rest_get("/api/v3/klines", {"symbol": sym, "interval": interval, "limit": int(limit)})

    def get_price(self, symbol: str) -> float:
        sym = self._normalize_symbol(symbol)
        data = self._rest_get("/api/v3/ticker/price", {"symbol": sym})
        return float(data["price"])

    # ------------------------ Symbol Filters (qty/price rounding) ------------------------

    def _load_symbol_filters(self, symbol: str):
        sym = self._normalize_symbol(symbol)
        if sym in self._symbol_filters:
            return
        info = self._rest_get("/api/v3/exchangeInfo", {"symbol": sym})
        if not info or "symbols" not in info or not info["symbols"]:
            return
        sym_info = info["symbols"][0]
        lot_size = {}
        for f in sym_info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                lot_size["minQty"] = float(f["minQty"])
                lot_size["maxQty"] = float(f["maxQty"])
                lot_size["stepSize"] = float(f["stepSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                lot_size["minNotional"] = float(f["minNotional"])
            elif f["filterType"] == "PRICE_FILTER":
                lot_size["tickSize"] = float(f["tickSize"])
        self._symbol_filters[sym] = lot_size

    def round_qty(self, symbol: str, qty: float) -> float:
        """تقريب الكمية حسب stepSize وminQty."""
        sym = self._normalize_symbol(symbol)
        self._load_symbol_filters(sym)
        f = self._symbol_filters.get(sym, {})
        step = f.get("stepSize", 0.0)
        min_qty = f.get("minQty", 0.0)

        if qty <= 0:
            return 0.0
        if step and step > 0:
            # round down to step
            precision = int(round(-math.log10(step))) if step < 1 else 0
            # avoid floating issues
            rounded = math.floor(qty / step) * step
            qty = float(f"{rounded:.{max(0, precision)}f}")

        if min_qty and qty < min_qty:
            return 0.0
        return qty

    def _enforce_min_notional(self, symbol: str, price: float, qty: float) -> float:
        """لو القيمة أقل من minNotional يرجّع 0 (رفض الأمر)."""
        sym = self._normalize_symbol(symbol)
        self._load_symbol_filters(sym)
        f = self._symbol_filters.get(sym, {})
        min_notional = f.get("minNotional", 0.0)
        if min_notional and (price * qty) < min_notional:
            return 0.0
        return qty

    # ------------------------ Balances (cash) ------------------------

    def get_cash_balance(self, asset: str = "USDT") -> float:
        """
        - في DRY-RUN: يرجّع paper_cash من config.
        - في LIVE: يقرأ من حساب Binance.
        """
        if self._bnc is None:
            return float(self.cfg.get("trade", {}).get("paper_cash", 10000.0))
        try:
            acc = self._bnc.get_asset_balance(asset=asset)
            if not acc:
                return 0.0
            free = float(acc.get("free", 0.0))
            locked = float(acc.get("locked", 0.0))
            return free + locked
        except Exception as e:
            self.reporter.log(f"[Exchange] get_cash_balance error: {e}")
            return 0.0

    # ------------------------ Orders (Live or Simulated) ------------------------

    def create_market_order(self, symbol: str, side: str, quantity: float) -> Dict[str, Any]:
        """
        أمر ماركت موحّد:
        - في DRY-RUN: إرجاع أمر وهمي مع log.
        - في LIVE: استخدام python-binance.
        * يراعي round_qty وminNotional.
        """
        sym = self._normalize_symbol(symbol)
        price = self.get_price(sym)
        qty = self.round_qty(sym, float(quantity))
        qty = self._enforce_min_notional(sym, price, qty)

        if qty <= 0:
            msg = f"[Order] Rejected {side} {sym}: qty too small or < minNotional"
            self.reporter.log(msg)
            return {"status": "REJECTED", "symbol": sym, "side": side, "origQty": quantity, "executedQty": 0.0, "price": price}

        if self._bnc is None:
            # DRY-RUN
            self.reporter.log(f"[Paper] {side} {sym} qty={qty} @~{price}")
            return {
                "status": "FILLED",
                "symbol": sym,
                "side": side,
                "type": ORDER_TYPE_MARKET,
                "transactTime": int(time.time() * 1000),
                "price": price,
                "origQty": qty,
                "executedQty": qty,
            }

        try:
            order = self._bnc.create_order(
                symbol=sym,
                side=SIDE_BUY if side.upper() == "BUY" else SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            self.reporter.log(f"[Live] {side} {sym} qty={qty}")
            return order
        except Exception as e:
            self.reporter.log(f"[Exchange] create_market_order error: {e}")
            return {"status": "ERROR", "error": str(e)}

    # sugar helpers
    def buy_market(self, symbol: str, quantity: float) -> Dict[str, Any]:
        return self.create_market_order(symbol, "BUY", quantity)

    def sell_market(self, symbol: str, quantity: float) -> Dict[str, Any]:
        return self.create_market_order(symbol, "SELL", quantity)
