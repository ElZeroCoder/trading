# exit.py
import time
import asyncio

class ExitManager:
    def __init__(self, cfg: dict, reporter, exchange, portfolio, strategy_mgr):
        self.cfg = cfg
        self.reporter = reporter
        self.exchange = exchange
        self.portfolio = portfolio
        self.strategy_mgr = strategy_mgr
        self.trailing = {}  # symbol -> highest

        tcfg = cfg.get("trade", {})
        self.dry_run = bool(tcfg.get("dry_run", True))

    async def manage_positions(self):
        open_positions = self.portfolio.get_open_positions()
        for pos in list(open_positions.values()):
            try:
                price = self.exchange.get_price(pos["symbol"])

                highest = self.trailing.get(pos["symbol"], pos["entry_price"])
                if price > highest:
                    self.trailing[pos["symbol"]] = price
                trail_pct = self.cfg["trade"].get("trailing_stop_pct", 1.0) / 100.0
                trail_stop = self.trailing.get(pos["symbol"], price) * (1 - trail_pct)

                pnl = (price - pos["entry_price"]) * pos["qty"]
                pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100.0

                reason = None
                if price >= pos["tp_price"]:
                    reason = "TP hit"
                elif price <= pos["sl_price"] or price <= trail_stop:
                    reason = "SL/Trail hit"

                if reason:
                    await self._close_position(pos, price, pnl, pnl_pct, reason)

            except Exception as e:
                self.reporter.log(f"[Exit] {e}")

    async def _close_position(self, pos, mkt_price, pnl, pnl_pct, reason: str):
        # تنفيذ بيع (ماركت): live أو paper
        exec_qty, exec_price = await self._execute_or_paper_sell(pos["symbol"], pos["qty"])

        status = "✅ PROFIT" if pnl > 0 else "❌ LOSS"
        await self._notify(
            f"🔴 SELL {pos['symbol']} @ {exec_price:.6f} | Strategy: {pos['strategy']}\n"
            f"Reason: {reason}\n"
            f"{status} | PnL: {pnl:.2f} USDT ({pnl_pct:.2f}%)"
        )

        trade = {
            "symbol": pos["symbol"],
            "strategy": pos["strategy"],
            "entry_price": pos["entry_price"],
            "exit_price": exec_price or mkt_price,
            "qty": exec_qty or pos["qty"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "ts_entry": pos["entry_ts"],
            "ts_exit": time.time(),
        }
        self.reporter.save_trade(trade)
        self.portfolio.close_position(pos["symbol"])

    async def _execute_or_paper_sell(self, symbol: str, qty: float):
        try:
            order = self.exchange.sell_market(symbol, qty)
            status = str(order.get("status", "FILLED")).upper()
            if status in ("FILLED", "PARTIALLY_FILLED"):
                exec_qty = float(order.get("executedQty", qty))
                price = float(order.get("price", self.exchange.get_price(symbol)))
                return exec_qty, price
            else:
                self.reporter.log(f"[EXEC] Sell rejected: {order}")
                return 0.0, 0.0
        except Exception as e:
            self.reporter.log(f"[EXEC] Sell error: {e}")
            return 0.0, 0.0

    async def _notify(self, text: str):
        if hasattr(self.reporter, "notify") and asyncio.iscoroutinefunction(self.reporter.notify):
            await self.reporter.notify(text)
        else:
            self.reporter.log(f"[NOTIFY] {text}")
