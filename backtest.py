# backtest.py
from __future__ import annotations
import os, json, time
from typing import Optional, List, Dict, Tuple
import numpy as np, pandas as pd

class Backtester:
    def __init__(self, cfg: dict, reporter, exchange_client):
        self.cfg = cfg
        self.reporter = reporter
        self.exchange = exchange_client

        t = cfg.get("trade", {}) or {}
        b = cfg.get("backtest", {}) or {}

        self.initial_balance = float(t.get("initial_balance", 1000.0))
        self.strategy_weights = {
            "momentum": float(b.get("weight_momentum", 0.5)),
            "ma":       float(b.get("weight_ma", 0.5)),
        }
        # نسب الاستراتيجية
        sw_sum = sum(self.strategy_weights.values()) or 1.0
        for k in self.strategy_weights:
            self.strategy_weights[k] = self.strategy_weights[k] / sw_sum

        self.per_trade_pct     = float(t.get("per_trade_pct", 10.0)) / 100.0
        self.take_profit_pct   = float(t.get("take_profit_pct", 5.0)) / 100.0
        self.stop_loss_pct     = float(t.get("stop_loss_pct", 2.0)) / 100.0
        self.trailing_stop_pct = float(t.get("trailing_stop_pct", 1.0)) / 100.0
        self.taker_fee_pct     = float(t.get("taker_fee_pct", 0.0004))
        self.slippage_pct      = float(t.get("slippage_pct", 0.0005))
        self.min_atr_pct       = float(t.get("min_atr_pct", 0.0005))

        # تحكم في اللوج من config.backtest
        self.verbose_entries   = bool(b.get("verbose_entries", False))   # اطبع كل ENTRY/EXIT
        self.verbose_forced    = bool(b.get("verbose_forced", False))    # اطبع FORCE EXIT
        self.verbose_summary   = bool(b.get("verbose_summary", True))    # اطبع ملخص نهائي

        # قلّل الدخولات المتكررة: Cooldown بالشموع لكل استراتيجية/رمز
        self.entry_cooldown_bars = int(b.get("entry_cooldown_bars", 10))  # مثال: 10 شموع

        # طول البيانات
        self.interval      = str(t.get("interval", "1m"))
        self.max_bars      = int(b.get("max_bars", 2000))

        # مجلد الإخراج
        self.out_dir = os.path.join(os.path.dirname(__file__), "backtest_output")
        os.makedirs(self.out_dir, exist_ok=True)

    # ---------------- Helpers ----------------
    def _bt_log(self, text: str, kind: str = "ENTRY"):
        """طباعة مشروطة حسب الـ verbose."""
        if kind == "ENTRY" and not self.verbose_entries:
            return
        if kind == "EXIT" and not self.verbose_entries:
            return
        if kind == "FORCE" and not self.verbose_forced:
            return
        self.reporter.log(text, level="INFO")

    def _klines_to_df(self, kl):
        if not kl: return pd.DataFrame()
        df = pd.DataFrame(kl, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_av","trades","tb_base_av","tb_quote_av","ignore"
        ])
        df["open_time"]  = pd.to_datetime(df["open_time"], unit="ms")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
        return df

    def _atr_pct(self, df: pd.DataFrame, lookback=14) -> Optional[float]:
        if len(df) < lookback + 1: return None
        h,l,c = df["high"].values, df["low"].values, df["close"].values
        tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
        atr = float(np.mean(tr[-lookback:]))
        last = float(c[-1])
        return None if last<=0 else atr/last

    def _sig_momentum(self, df: pd.DataFrame, lookback_min: int, change_pct: float) -> bool:
        if len(df) < lookback_min + 1: return False
        start = df["close"].iloc[-(lookback_min+1)]
        last  = df["close"].iloc[-1]
        if start <= 0: return False
        pct = (last - start)/start*100
        return pct >= change_pct

    def _sig_ma(self, df: pd.DataFrame, s=9, l=21) -> bool:
        if len(df) < l: return False
        ms = df["close"].iloc[-s:].mean()
        ml = df["close"].iloc[-l:].mean()
        return ms > ml

    # ---------------- Core ----------------
    def run_backtest(self, symbol: str, interval=None, max_bars=None) -> Dict:
        interval = interval or self.interval
        max_bars = max_bars or self.max_bars

        try:
            kl = self.exchange.get_klines(symbol, interval=interval, limit=max_bars)
        except Exception as e:
            self.reporter.log(f"[Backtest] fetch error {symbol}: {e}", level="ERROR")
            return {}
        df = self._klines_to_df(kl)
        if df.empty:
            self.reporter.log(f"[Backtest] no data {symbol}", level="WARNING")
            return {}

        balances = {
            "momentum": self.initial_balance * self.strategy_weights["momentum"],
            "ma":       self.initial_balance * self.strategy_weights["ma"],
        }
        open_positions: List[Dict] = []
        last_entry_bar: Dict[Tuple[str,str], int] = {}  # (symbol,strategy) -> bar index

        equity_curve, ts = [], []
        trades_count = {"momentum": 0, "ma": 0}

        for i in range(len(df)):
            win = df.iloc[:i+1]
            t  = win["close_time"].iloc[-1] if "close_time" in win.columns else win["open_time"].iloc[-1]
            px = float(win["close"].iloc[-1])

            # exits first
            keep = []
            for pos in open_positions:
                pos["highest"] = max(pos.get("highest", pos["entry_price"]), px)
                trail = pos["highest"] * (1 - self.trailing_stop_pct)
                reason, ex_px = None, None
                if px >= pos["tp_price"]:
                    reason, ex_px = "TP", px
                elif px <= pos["sl_price"]:
                    reason, ex_px = "SL", px
                elif px <= trail:
                    reason, ex_px = "TRAIL", px

                if reason:
                    proceeds = ex_px * pos["qty"]
                    fee_exit = proceeds * self.taker_fee_pct
                    balances[pos["strategy"]] += (proceeds - fee_exit)
                    pnl = (proceeds - fee_exit) - (pos["entry_price"]*pos["qty"] + pos["entry_fee"])
                    self._bt_log(f"[BT EXIT] {symbol} {pos['strategy']} {reason} @ {ex_px:.4f} pnl={pnl:.2f}", kind="EXIT")
                else:
                    keep.append(pos)
            open_positions = keep

            # equity snapshot
            total_cash = sum(balances.values())
            mkt_value  = sum(p["qty"] * px for p in open_positions)
            equity_curve.append(total_cash + mkt_value)
            ts.append(t.timestamp())

            # evaluate entries (with cooldown + ATR filter)
            atrp = self._atr_pct(win, 14)
            if (atrp is None) or (atrp < self.min_atr_pct):
                continue

            def can_enter(strategy_name: str) -> bool:
                key = (symbol, strategy_name)
                last_i = last_entry_bar.get(key, -10**9)
                return (i - last_i) >= self.entry_cooldown_bars and (not any(p["strategy"]==strategy_name for p in open_positions))

            def do_enter(strategy_name: str):
                nonlocal i
                alloc_pool = balances[strategy_name]
                alloc = alloc_pool * self.per_trade_pct
                if alloc <= 0:
                    return False
                entry = px * (1 + self.slippage_pct)
                qty   = alloc / entry
                fee_e = entry * qty * self.taker_fee_pct
                need  = entry * qty + fee_e
                if need > balances[strategy_name]:
                    qty = balances[strategy_name] / (entry * (1 + self.taker_fee_pct))
                    fee_e = entry * qty * self.taker_fee_pct
                    need  = entry * qty + fee_e
                if qty <= 0 or need > balances[strategy_name]:
                    return False

                tp = entry * (1 + self.take_profit_pct)
                sl = entry * (1 - self.stop_loss_pct)
                balances[strategy_name] -= need
                open_positions.append({
                    "strategy": strategy_name, "entry_price": entry, "qty": qty,
                    "entry_time": t, "entry_index": i, "tp_price": tp, "sl_price": sl,
                    "highest": entry, "entry_fee": fee_e
                })
                last_entry_bar[(symbol, strategy_name)] = i
                trades_count[strategy_name] += 1
                self._bt_log(f"[BT ENTRY] {symbol} {strategy_name} @ {entry:.4f} qty={qty:.6f} fee={fee_e:.4f}", kind="ENTRY")
                return True

            # momentum
            try:
                if can_enter("momentum") and self._sig_momentum(win,
                        int(self.cfg["trade"].get("entry_lookback_min",15)),
                        float(self.cfg["trade"].get("entry_change_pct",3.0))):
                    do_enter("momentum")
            except Exception:
                pass

            # ma
            try:
                if can_enter("ma") and self._sig_ma(win, 9, 21):
                    do_enter("ma")
            except Exception:
                pass

        # force exit all
        last_px = float(df["close"].iloc[-1])
        for pos in open_positions:
            ex_px = last_px * (1 - self.slippage_pct)
            proceeds = ex_px * pos["qty"]
            fee_exit = proceeds * self.taker_fee_pct
            balances[pos["strategy"]] += (proceeds - fee_exit)
            pnl = (proceeds - fee_exit) - (pos["entry_price"]*pos["qty"] + pos["entry_fee"])
            self._bt_log(f"[BT FORCE EXIT] {symbol} {pos['strategy']} @ {ex_px:.4f} pnl={pnl:.2f}", kind="FORCE")

        eq = np.array(equity_curve)
        peak = np.maximum.accumulate(eq) if len(eq) else eq
        dd   = (peak - eq)/peak if len(eq) else np.array([0.0])
        max_dd = float(np.nanmax(dd)) if len(eq) else 0.0
        final_total = sum(balances.values())
        summary = {
            "symbol": symbol,
            "initial_balance": self.initial_balance,
            "final_balance": final_total,
            "total_pnl": final_total - self.initial_balance,
            "max_drawdown_pct": round(max_dd*100,2),
            "trades": trades_count,
        }

        # save json summary
        try:
            with open(os.path.join(self.out_dir, f"summary_{symbol}_{int(time.time())}.json"),"w",encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        if self.verbose_summary:
            self.reporter.log(
                f"[Backtest] {symbol} | PnL={summary['total_pnl']:.2f} | MaxDD={summary['max_drawdown_pct']}% | "
                f"Trades: mom={trades_count['momentum']}, ma={trades_count['ma']}",
                level="INFO"
            )

        return {"summary": summary, "equity": list(eq)}
