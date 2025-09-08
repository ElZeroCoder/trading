# Pro Trading Bot (Full)

Files:
- main.py: entry point
- core.py: exchange wrapper & execution
- storage.py: sqlite storage
- strategies.py: strategies
- risk.py: risk manager
- filters.py: filtering
- exit.py: exit management
- portfolio.py: DCA & rebalancing
- ml.py: ML scaffolding
- backtest.py: backtesting scaffolding
- reporter.py: logging & telegram
- dashboard.py: streamlit dashboard

## Setup
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

Edit config.yaml, keep simulation/dry_run true until tested.

Run: python main.py --config config.yaml
Run dashboard: streamlit run dashboard.py
