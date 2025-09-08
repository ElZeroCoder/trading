import time
import json
import os

class PortfolioManager:
    def __init__(self, cfg: dict, reporter, exchange, strategy_mgr, state_file="portfolio.json"):
        self.cfg = cfg
        self.reporter = reporter
        self.exchange = exchange
        self.strategy_mgr = strategy_mgr
        self.last_rebalance = 0
        self.state_file = state_file

        # تحميل الحالة من ملف (لو موجود)
        self.state = {
            "positions": {},   # pid -> dict
            "balances": {},    # strategy -> balance
            "ledger": []       # list of events
        }
        self._load_state()

    # --- تحميل/حفظ
    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception:
                self.reporter.log("[Portfolio] Failed to load state file")

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            self.reporter.log(f"[Portfolio] Failed to save state: {e}")

    # --- إدارة الأرصدة
    def update_balance(self, pnl: float, strategy: str):
        bal = self.state["balances"].get(strategy, self.cfg["trade"].get("initial_balance", 1000) / 2)
        new_bal = bal + pnl
        self.state["balances"][strategy] = new_bal
        self._save_state()
        self.reporter.log(f"[Portfolio] Balance {strategy} updated: {bal:.2f} -> {new_bal:.2f}")

    def get_balance(self, strategy: str) -> float:
        return self.state["balances"].get(strategy, 0.0)

    # --- إدارة الصفقات
    def save_position(self, pos: dict) -> str:
        pid = str(int(time.time() * 1000))  # ID بسيط من التوقيت
        pos["status"] = "open"
        self.state["positions"][pid] = pos
        self._save_state()
        return pid

    def close_position(self, pid: str, pnl: float):
        if pid in self.state["positions"]:
            self.state["positions"][pid]["status"] = "closed"
            strategy = self.state["positions"][pid].get("strategy", "default")
            self.update_balance(pnl, strategy)
            self.ledger("exit", f"pid={pid} closed pnl={pnl:.2f}")
            self._save_state()

    # --- Ledger
    def ledger(self, event: str, details: str):
        entry = {"ts": time.time(), "event": event, "details": details}
        self.state["ledger"].append(entry)
        self._save_state()
        self.reporter.log(f"[Ledger] {event}: {details}")

    # --- Rebalancing
    def rebalance_if_needed(self):
        now = time.time()
        if now - self.last_rebalance < 3600 * 24:
            return
        self.last_rebalance = now
        self.reporter.log("Running rebalance (placeholder)")

    # --- DCA logic
    def add_dca(self, pid, levels=(0.95, 0.90, 0.85)):
        if pid not in self.state["positions"]:
            return
        pos = self.state["positions"][pid]
        self.reporter.log(f"[DCA] Check DCA for pid={pid} {pos['symbol']}")

    # --- دوال مطلوبة للبوت ---
    def get_open_positions(self):
        """ارجع الصفقات المفتوحة"""
        return {pid: pos for pid, pos in self.state["positions"].items() if pos.get("status") != "closed"}

    def register_trade(self, trade: dict):
        """دالة stub لاستبدال Storage.register_trade"""
        self.save_position(trade)
        self.reporter.log(f"[Portfolio] Trade registered: {trade}")
def get_open_positions_summary(self) -> str:
    """ترجع ملخص كل الصفقات المفتوحة كنص"""
    open_pos = self.get_open_positions()
    if not open_pos:
        return "لا توجد صفقات مفتوحة حالياً."
    
    lines = []
    for pid, pos in open_pos.items():
        lines.append(
            f"PID={pid} | {pos['symbol']} | {pos['strategy']} | "
            f"Entry={pos['entry_price']:.4f} | Qty={pos['qty']:.6f}"
        )
    return "\n".join(lines)