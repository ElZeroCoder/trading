#!/usr/bin/env python3
import argparse, asyncio, os, yaml
from typing import List

from modules.reporter import Reporter
from modules.core import ExchangeClient
from modules.risk import RiskManager
from modules.filters import FilterManager
from modules.strategies import StrategyManager
from modules.exit import ExitManager
from modules.portfolio import PortfolioManager
from modules.ml import MLModule
from modules.backtest import Backtester


def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run_live_or_paper(cfg):
    """التشغيل العادي: Paper أو Live حسب trade.dry_run"""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)

    reporter = Reporter(cfg, data_dir=data_dir)
    exchange = ExchangeClient(cfg, reporter)

    portfolio = PortfolioManager(cfg, reporter, exchange)
    risk = RiskManager(cfg, reporter, portfolio, exchange)
    strategy_mgr = StrategyManager(cfg, reporter, exchange, risk, portfolio)
    exit_mgr = ExitManager(cfg, reporter, exchange, portfolio, strategy_mgr)
    filters = FilterManager(cfg, reporter, exchange)
    ml = MLModule(cfg, reporter, state_file=os.path.join(data_dir, "ml_state.json"))

    await reporter.notify(
        f"🚀 Bot starting | dry_run={cfg['trade'].get('dry_run', True)} | testnet={cfg.get('binance', {}).get('testnet', False)}"
    )

    universe = filters.fetch_universe()
    reporter.log(f"Universe size: {len(universe)} symbols")

    last_scan = 0.0
    poll_sec = int(cfg["trade"].get("poll_interval_sec", 30))
    running = True

    while running:
        try:
            now = asyncio.get_running_loop().time()
            if now - last_scan >= poll_sec:
                await strategy_mgr.scan_and_trade(universe)
                last_scan = now

            await exit_mgr.manage_positions()
            portfolio.rebalance_if_needed()
            ml.train_if_needed()

            await asyncio.sleep(1)

        except KeyboardInterrupt:
            await reporter.notify("🛑 Stopping by user")
            running = False
        except Exception as e:
            await reporter.notify(f"[Main loop error] {e}")
            await asyncio.sleep(3)


def run_backtest(cfg):
    """وضع الباكتيست: يشغّل Backtester على الـ universe ويحفظ النتائج ثم يخرج"""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)

    reporter = Reporter(cfg)
    exchange = ExchangeClient(cfg, reporter)
    backtester = Backtester(cfg, reporter, exchange)

    # نفس طريقة الحصول على الـ universe زي التشغيل العادي
    filters = FilterManager(cfg, reporter, exchange)
    universe: List[str] = filters.fetch_universe()
    if not universe:
        universe = ["BTCUSDT"]

    reporter.log(f"[Backtest] Running on {len(universe)} symbol(s): {', '.join(universe[:10])}{' ...' if len(universe)>10 else ''}")

    results = []
    for sym in universe:
        try:
            res = backtester.run_backtest(symbol=sym)
            if res:
                results.append(res.get("summary", {}))
        except Exception as e:
            reporter.log(f"[Backtest] Error for {sym}: {e}")

    # تقرير ختامي بسيط في اللوج
    total = len(results)
    wins = sum(1 for s in results if s.get("total_pnl", 0) > 0)
    reporter.log(f"[Backtest] Done. Symbols={total}, Positive PnL={wins}")

    # تنبيه على التليجرام (اختياري)
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(reporter.notify(f"✅ Backtest finished. Symbols={total}, Positive={wins}"))
        loop.close()
    except Exception:
        pass


async def main_async(cfg_path: str):
    cfg = load_config(cfg_path)
    simulation = bool(cfg.get("trade", {}).get("simulation", False))

    if simulation:
        # وضع الباكتيست (مرة واحدة)
        run_backtest(cfg)
        return
    else:
        # وضع التشغيل العادي (Paper/Live)
        await run_live_or_paper(cfg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pro Trading Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.config))
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main_async(args.config))
