# Polymarket Tail Sweeper

A local, Windows-first Python desktop application that automates a long-tail Polymarket scavenger strategy: scanning live markets, identifying ultra-cheap neglected contracts, placing tiny systematic buy orders, and auto-selling in tranches when prices reprice upward.

## Quick Start (Windows)

1. **Install Python 3.10+** from [python.org](https://www.python.org/downloads/). Make sure to check "Add Python to PATH" during installation.

2. **Double-click `launcher.bat`**. It will:
   - Detect your Python installation
   - Create a `.venv` virtual environment (first run)
   - Install all dependencies (first run)
   - Create a `.env` file from `.env.example` (first run)
   - Launch the GUI

3. The app opens in **Paper Mode** by default — no credentials needed. It uses real Polymarket market data but simulates all order fills.

## Quick Start (Linux / macOS)

```bash
cd polymarket_tail_sweeper
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## How Paper Mode Works

- **Real market data**: The app fetches live markets and order books from Polymarket's public APIs.
- **Conservative fill simulation**: Buy orders only fill immediately if the order price crosses the visible top-of-book ask with adequate displayed size. Otherwise, orders rest and are periodically checked against the live book.
- **Simulated positions**: Positions are tracked locally with cost basis and average entry.
- **Real PnL tracking**: Unrealized PnL updates using live midpoint prices. Realized PnL is computed on simulated sells.
- **Exit ladder**: When a position's mark reaches configured multiples (default 3x, 5x, 10x), the bot automatically sells partial tranches.

Paper mode is the default and requires no configuration.

## Live Mode

To enable live trading:

1. Edit the `.env` file (or paste credentials into the Settings tab):
   - `POLYMARKET_PRIVATE_KEY` — your Ethereum private key
   - `POLYMARKET_FUNDER_ADDRESS` — your Polygon wallet/proxy address
   - `POLYMARKET_SIGNATURE_TYPE` — 0 for EOA, 1 for Gnosis Safe, 2 for Proxy

2. In the Settings tab, uncheck "Paper Mode".

3. Click "Start Bot". The app will:
   - Validate credentials
   - Perform a geoblock check
   - Initialize the py-clob-client SDK
   - Begin live order placement

**Safety gates**: Live mode refuses to start if credentials are missing or geoblock is detected.

## Project Structure

```
polymarket_tail_sweeper/
├── launcher.bat          # Windows one-click launcher
├── main.py               # Application entry point
├── config.py             # Settings and configuration
├── requirements.txt      # Python dependencies
├── .env.example          # Template for credentials
├── README.md             # This file
├── gui/
│   ├── main_window.py    # Main application window
│   ├── dashboard.py      # Dashboard metric cards
│   ├── tables.py         # Data table widgets
│   ├── settings_tab.py   # Settings editor
│   └── styles.py         # Dark theme stylesheet
├── bot/
│   ├── bot_worker.py     # Background scan/trade worker
│   ├── strategy.py       # Entry/exit strategy logic
│   └── pnl.py            # PnL engine (FIFO accounting)
├── adapters/
│   ├── polymarket_public.py   # Public market data adapter
│   └── polymarket_trade.py    # Trading adapter (paper + live)
├── storage/
│   └── database.py       # SQLite persistence layer
├── models/
│   └── data_models.py    # Core data models
└── utils/
    └── logging_utils.py  # Logging configuration
```

## Configuration

All settings are editable in the GUI Settings tab and persisted to SQLite. Key defaults:

| Setting | Default | Description |
|---------|---------|-------------|
| Paper Mode | On | Simulated trading |
| Scan Interval | 60s | Time between scan cycles |
| Max Entry Price | $0.005 | Maximum price to pay for a contract |
| Min Spread | $0.001 | Minimum bid-ask spread required |
| Per-Order Size | $1.00 | USD amount per buy order |
| Max Exposure | $50.00 | Hard cap on total cost basis |
| Max Positions | 50 | Hard cap on open positions |
| Max Buys/Cycle | 3 | Limit new buys per scan cycle |
| Exit Ladder | 3x/5x/10x | Sell 25% at each multiple |

## Safety Controls

- **Hard exposure cap** — bot will not exceed max total exposure
- **Hard position cap** — limits total concurrent positions
- **Price guard** — rejects any entry above $0.05
- **Stale order cancellation** — auto-cancels orders older than timeout
- **Kill switch** — immediately cancels all orders and stops the bot
- **Geoblock check** — refuses live mode if API access is blocked
- **Duplicate guards** — prevents duplicate positions and orders on the same token
- **Error throttling** — stops bot after 5 consecutive cycle errors

## Logging

- **File**: `tailsweeper.log` in the application directory
- **GUI**: Event Log tab shows real-time log messages
- **Export**: Settings tab has an "Export Logs" button

## Known Limitations

1. **No WebSocket streaming** — market data is polled via REST. The adapter boundary is clean for future WebSocket integration.
2. **Sequential order book fetches** — for large market scans, order book requests are sequential with rate limiting. Batch/parallel fetching can be added.
3. **py-clob-client SDK** — live mode depends on the official SDK which may require specific versions. If import fails, the error is logged clearly.
4. **Tick sizes** — the current implementation uses raw float prices. Polymarket's variable tick sizes should be respected in production; the adapter boundary is prepared for this.
5. **No historical backtest** — this is a forward-looking scanner, not a backtester.

## License

For personal use. Not financial advice. Trade at your own risk.
