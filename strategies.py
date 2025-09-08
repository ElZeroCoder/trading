# strategies.py
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from ta.trend import MACD
from ta.momentum import RSIIndicator


class StrategyManager:
    def __init__(self, cfg: dict, reporter, exchange, risk, portfolio):
        self.cfg = cfg
        self.reporter = reporter
        self.exchange = exchange
        self.risk = risk
        self.portfolio = portfolio

        tcfg = cfg.get("trade", {})
        scfg = cfg.get("strategy", {})

        self.interval: str = tcfg.get("interval", "1m")
        self.lookback_min: int = int(tcfg.get("entry_lookback_min", 15))
        self.entry_change_pct: float = float(tcfg.get("entry_change_pct", 3.0))
        self.min_atr_pct: float = float(tcfg.get("min_atr_pct", 0.0005))

        self.ma_short: int = int(scfg.get("ma_short", 9))
        self.ma_long: int = int(scfg.get("ma_long", 21))
        self.rsi_period: int = int(scfg.get("rsi_period", 14))
        self.rsi_buy: float = float(scfg.get("rsi_buy", 55))
        self.rsi_sell: float = float(scfg.get("rsi_sell", 70))
        self.macd_fast: int = int(scfg.get("macd_fast", 12))
        self.macd_slow: int = int(scfg.get("macd_slow", 26))
        self.macd_signal: int = int(scfg.get("macd_signal", 9))
        self.use_rsi_filter: bool = bool(scfg.get("use_rsi_filter", True))
        self.use_macd_filter: bool = bool(scfg.get("use_macd_filter", True))

        self.STRAT_MOMENTUM = "momentum"
        self.STRAT_MA = "ma"

        self.dry_run = bool(tcfg.get("dry_run", True))  # مهم لتمييز live/paper

    # ------------------------- Data helpers -------------------------

    def _klines_to_df(self, klines) -> pd.DataFrame:
        if not klines:
            return pd.DataFrame()
        df = pd.DataFrame(
            klines,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_av",
                "trades",
                "tb_base_av",
                "tb_quote_av",
                "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        return df

    def _fetch_df(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        kl = self.exchange.get_klines(symbol, interval=self.interval, limit=limit)
        return self._klines_to_df(kl)

    # ------------------------- Indicators -------------------------

    def get_rsi(self, df: pd.DataFrame, period: Optional[int] = None) -> Optional[float]:
        p = int(period or self.rsi_period)
        if df is None or len(df) < p + 1:
            return None
        rsi = RSIIndicator(close=df["close"], window=p, fillna=False)
        val = rsi.rsi().iloc[-1]
        return float(val) if np.isfinite(val) else None

    def get_macd(
        self,
        df: pd.DataFrame,
        fast: Optional[int] = None,
        slow: Optional[int] = None,
        signal: Optional[int] = None,
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        f = int(fast or self.macd_fast)
        s = int(slow or self.macd_slow)
        g = int(signal or self.macd_signal)
        needed = max(f, s, g) + 5
        if df is None or len(df) < needed:
            return None, None, None
        macd = MACD(close=df["close"], window_slow=s, window_fast=f, window_sign=g)
        macd_line = macd.macd().iloc[-1]
        signal_line = macd.macd_signal().iloc[-1]
        hist = macd.macd_diff().iloc[-1]
        return (
            float(macd_line) if np.isfinite(macd_line) else None,
            float(signal_line) if np.isfinite(signal_line) else None,
            float(hist) if np.isfinite(hist) else None,
        )

    def get_ma(self, df: pd.DataFrame, window: int) -> Optional[float]:
        if df is None or len(df) < window:
            return None
        ma = df["close"].iloc[-window:].mean()
        return float(ma) if np.isfinite(ma) else None

    # ------------------------- Signals -------------------------

    def signal_momentum(self, df: pd.DataFrame) -> bool:
        if df is None or len(df) < self.lookback_min + 1:
            return False
        start = df["close"].iloc[-(self.lookback_min + 1)]
        last = df["close"].iloc[-1]
        if start <= 0:
            return False
        pct = (last - start) / start * 100.0
        return pct >= self.entry_change_pct

    def signal_ma_cross(self, df: pd.DataFrame) -> bool:
        ms = self.get_ma(df, self.ma_short)
        ml = self.get_ma(df, self.ma_long)
        if ms is None or ml is None:
            return False
        return ms > ml

    def rsi_filter_buy(self, df: pd.DataFrame) -> bool:
        if not self.use_rsi_filter:
            return True
        r = self.get_rsi(df)
        return (r is not None) and (r >= self.rsi_buy)

    def macd_filter_buy(self, df: pd.DataFrame) -> bool:
        if not self.use_macd_filter:
            return True
        m, s, h = self.get_macd(df)
        if m is None or s is None:
            return False
        return m > s

    # ------------------------- Trade Scan -------------------------

    async def scan_and_trade(self, universe: List[str]) -> None:
        if not universe:
            return

        if hasattr(self.risk, "check_daily_loss_guard"):
            if not self.risk.check_daily_loss_guard():
                return

        open_positions = {}
        try:
            open_positions = self.portfolio.get_open_positions()
        except Exception:
            open_positions = {}

        for symbol in universe:
            try:
                df = self._fetch_df(symbol, limit=max(300, self.ma_long + 10))
                if df.empty:
                    continue

                has_momentum = any(
                    (p.get("symbol") == symbol and p.get("strategy") == self.STRAT_MOMENTUM)
                    for p in open_positions.values()
                )
                has_ma = any(
                    (p.get("symbol") == symbol and p.get("strategy") == self.STRAT_MA)
                    for p in open_positions.values()
                )

                price = float(df["close"].iloc[-1])

                # ===== Strategy 1: Momentum =====
                if not has_momentum and self.signal_momentum(df) and self.rsi_filter_buy(df) and self.macd_filter_buy(df):
                    sl_price, tp_price = self.risk.compute_sl_tp_prices(price)
                    qty = self.risk.compute_position_size_by_risk(
                        strategy=self.STRAT_MOMENTUM,
                        entry_price=price,
                        stop_price=sl_price,
                        symbol=symbol,
                    )
                    if qty > 0:
                        exec_qty, exec_price = await self._execute_or_paper_buy(symbol, qty)
                        if exec_qty > 0:
                            self.portfolio.open_position(
                                symbol=symbol,
                                strategy=self.STRAT_MOMENTUM,
                                entry_price=exec_price,
                                qty=exec_qty,
                                sl_price=sl_price,
                                tp_price=tp_price,
                            )
                            await self._notify(f"🟢 BUY {symbol} @ {exec_price:.6f} | {self.STRAT_MOMENTUM}\n"
                                               f"SL: {sl_price:.6f} | TP: {tp_price:.6f} | QTY: {exec_qty:.6f}")

                # ===== Strategy 2: MA Cross =====
                if not has_ma and self.signal_ma_cross(df) and self.rsi_filter_buy(df) and self.macd_filter_buy(df):
                    sl_price, tp_price = self.risk.compute_sl_tp_prices(price)
                    qty = self.risk.compute_position_size_by_risk(
                        strategy=self.STRAT_MA,
                        entry_price=price,
                        stop_price=sl_price,
                        symbol=symbol,
                    )
                    if qty > 0:
                        exec_qty, exec_price = await self._execute_or_paper_buy(symbol, qty)
                        if exec_qty > 0:
                            self.portfolio.open_position(
                                symbol=symbol,
                                strategy=self.STRAT_MA,
                                entry_price=exec_price,
                                qty=exec_qty,
                                sl_price=sl_price,
                                tp_price=tp_price,
                            )
                            await self._notify(f"🟢 BUY {symbol} @ {exec_price:.6f} | {self.STRAT_MA}\n"
                                               f"SL: {sl_price:.6f} | TP: {tp_price:.6f} | QTY: {exec_qty:.6f}")

            except Exception as e:
                self.reporter.log(f"[Strategy][{symbol}] {e}")

    # ------------------------- Execution helpers -------------------------

    async def _notify(self, text: str):
        if hasattr(self.reporter, "notify") and asyncio.iscoroutinefunction(self.reporter.notify):
            await self.reporter.notify(text)
        else:
            self.reporter.log(f"[NOTIFY] {text}")

    async def _execute_or_paper_buy(self, symbol: str, qty: float) -> Tuple[float, float]:
        """لو dry_run=False + مفاتيح → أمر ماركت حقيقي؛ غير كده → paper."""
        try:
            order = self.exchange.buy_market(symbol, qty)
            status = str(order.get("status", "FILLED")).upper()
            if status in ("FILLED", "PARTIALLY_FILLED"):
                exec_qty = float(order.get("executedQty", qty))
                price = float(order.get("price", self.exchange.get_price(symbol)))
                return exec_qty, price
            else:
                self.reporter.log(f"[EXEC] Buy rejected: {order}")
                return 0.0, 0.0
        except Exception as e:
            self.reporter.log(f"[EXEC] Buy error: {e}")
            return 0.0, 0.0
