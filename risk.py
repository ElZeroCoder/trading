# risk.py
from __future__ import annotations
from typing import Optional, Tuple, Any
import math, asyncio, time
from datetime import datetime, timezone

class RiskManager:
    def __init__(self, cfg: dict, reporter: Any, portfolio: Any, exchange: Any):
        self.cfg = cfg
        self.reporter = reporter
        self.portfolio = portfolio
        self.exchange = exchange

        tcfg = cfg.get("trade", {}) or {}
        rcfg = cfg.get("risk", {}) or {}

        self.per_trade_alloc_pct = float(tcfg.get("per_trade_pct", 10.0)) / 100.0
        self.per_trade_risk_pct  = float(rcfg.get("per_trade_risk_pct", 1.0)) / 100.0
        self.take_profit_pct = float(tcfg.get("take_profit_pct", 5.0)) / 100.0
        self.stop_loss_pct   = float(tcfg.get("stop_loss_pct", 2.0)) / 100.0
        self.taker_fee_pct   = float(tcfg.get("taker_fee_pct", 0.0004))

        self.max_daily_drawdown_pct = float(rcfg.get("max_daily_drawdown_pct", 0.0)) / 100.0
        self._day_anchor_ts = 0.0
        self._day_start_equity: Optional[float] = None

    # ------------- helpers -------------
    def _safe_notify(self, text: str, markdown: bool=False):
        try:
            nf = getattr(self.reporter, "notify", None)
            if nf and asyncio.iscoroutinefunction(nf):
                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        asyncio.create_task(self.reporter.notify(text, markdown=markdown))
                    else:
                        asyncio.run(self.reporter.notify(text, markdown=markdown))
                    return
                except RuntimeError:
                    asyncio.run(self.reporter.notify(text, markdown=markdown))
                    return
        except Exception:
            pass
        ns = getattr(self.reporter, "notify_sync", None)
        if callable(ns):
            try:
                ns(text, markdown=markdown)
                return
            except Exception:
                pass
        if hasattr(self.reporter, "log"):
            self.reporter.log(f"[NotifyFallback] {text}")

    def _round_qty(self, symbol: Optional[str], qty: float) -> float:
        if qty <= 0:
            return 0.0
        rounder = getattr(self.exchange, "round_qty", None)
        if callable(rounder) and symbol:
            try: return float(rounder(symbol, qty))
            except Exception: return float(qty)
        q = float(f"{qty:.8f}")
        return 0.0 if q < 1e-8 else q

    # ------------- public -------------
    def compute_sl_tp_prices(self, entry_price: float,
                             take_profit_pct: Optional[float]=None,
                             stop_loss_pct: Optional[float]=None) -> Tuple[float,float]:
        tp_pct = self.take_profit_pct if take_profit_pct is None else float(take_profit_pct)
        sl_pct = self.stop_loss_pct   if stop_loss_pct   is None else float(stop_loss_pct)
        tp_price = entry_price * (1.0 + tp_pct)
        sl_price = entry_price * (1.0 - sl_pct)
        return sl_price, tp_price

    def compute_position_size_by_risk(self, strategy: str, entry_price: float, stop_price: float,
                                      symbol: Optional[str]=None, balance_override: Optional[float]=None) -> float:
        # balance
        if balance_override is not None:
            balance = float(balance_override)
        else:
            get_bal = getattr(self.portfolio, "get_balance", None)
            balance = float(get_bal(strategy)) if callable(get_bal) else float(self.exchange.get_cash_balance())

        if balance <= 0 or entry_price <= 0:
            return 0.0

        risk_amount   = max(0.0, balance * self.per_trade_risk_pct)  # $
        risk_per_unit = abs(entry_price - stop_price)                # $/coin
        qty_risk = float("inf") if risk_per_unit <= 0 else (risk_amount / risk_per_unit)

        alloc_usdt = max(0.0, balance * self.per_trade_alloc_pct)
        qty_cap    = alloc_usdt / entry_price

        qty = max(0.0, min(qty_risk, qty_cap))
        qty = self._round_qty(symbol, qty)
        return qty

    def check_daily_loss_guard(self) -> bool:
        if self.max_daily_drawdown_pct <= 0:
            return True

        equity = None
        get_eq = getattr(self.portfolio, "get_total_equity", None)
        if callable(get_eq):
            try: equity = float(get_eq())
            except Exception: equity = None
        if equity is None:
            if hasattr(self.reporter, "log"):
                self.reporter.log("[Risk] Cannot read equity; skipping daily guard.")
            return True

        day_key_now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day_anchor_ts:
            anchor_key = datetime.fromtimestamp(self._day_anchor_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if anchor_key != day_key_now:
                self._day_start_equity = None
                self._day_anchor_ts = 0.0

        if self._day_start_equity is None:
            self._day_start_equity = equity
            self._day_anchor_ts = time.time()
            return True

        limit_eq = self._day_start_equity * (1.0 - self.max_daily_drawdown_pct)
        if equity <= limit_eq:
            self._safe_notify(
                f"⚠️ Daily loss guard: equity {equity:.2f} ≤ {limit_eq:.2f} "
                f"(start {self._day_start_equity:.2f}, maxDD {self.max_daily_drawdown_pct*100:.2f}%)."
            )
            if hasattr(self.reporter, "log"):
                self.reporter.log("[Risk] Trading halted for today.")
            return False
        return True
