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
- **Positions created on fill only**: A resting buy order does not create a position. Positions are only created or grown when actual (simulated) fills occur.
- **Reserved cash tracking**: Resting buy order notional is tracked as "cash reserved" and included in the committed capital calculation, preventing over-allocation.
- **Real PnL tracking**: Unrealized PnL updates using live midpoint prices. Realized PnL is computed on simulated sells.
- **Exit ladder**: When a position's mark reaches configured multiples (default 3x, 5x, 10x), the bot automatically sells partial tranches. Exit rungs only advance when the sell actually fills — not when the order is placed.

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

### Live order reconciliation

In live mode, the bot polls the exchange each cycle to reconcile local order state:
- Fetches all open orders from the CLOB
- Compares exchange `remaining_size` against local records
- Detects fill deltas and creates/updates positions accordingly
- Marks orders as FILLED, CANCELLED, or EXPIRED based on exchange state
- Orders that disappear from the exchange are marked CANCELLED

This polling-based approach works reliably without WebSocket user-fill streams.

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
    ├── logging_utils.py  # Logging configuration
    └── pricing.py        # Tick normalization and price safety
```

## Configuration

All settings are editable in the GUI Settings tab and persisted to SQLite. Key defaults:

| Setting | Default | Description |
|---------|---------|-------------|
| Paper Mode | On | Simulated trading |
| Scan Interval | 60s | Time between scan cycles |
| Market Refresh Interval | 300s | How often to re-fetch the full market list |
| Max Entry Price | $0.005 | Maximum price to pay for a contract |
| Min Spread | $0.001 | Minimum bid-ask spread required |
| Per-Order Size | $1.00 | USD amount per buy order |
| Max Exposure | $50.00 | Hard cap on total committed capital |
| Max Positions | 50 | Hard cap on open positions |
| Max Buys/Cycle | 3 | Limit new buys per scan cycle |
| Exit Ladder | 3x/5x/10x | Sell 25% at each multiple |

## Safety Controls

- **Committed capital gating** — new buys are blocked when filled positions + resting buy order notional would exceed max exposure
- **Hard position cap** — limits total concurrent positions
- **Price guard** — rejects any entry above $0.05
- **Tick normalization** — all live order prices are rounded to valid exchange increments
- **Post-only enforcement** — post-only buys are repriced below the ask if they would cross; skipped if no valid price exists
- **Stale order cancellation** — auto-cancels orders older than timeout
- **Duplicate order guards** — prevents duplicate buy and sell orders on the same token
- **Kill switch** — immediately cancels all orders and stops the bot
- **Geoblock check** — refuses live mode if API access is blocked
- **Error throttling** — stops bot after 5 consecutive cycle errors

## Logging

- **File**: `tailsweeper.log` in the application directory
- **GUI**: Event Log tab shows real-time log messages
- **Export**: Settings tab has an "Export Logs" button

## Known Limitations

1. **No WebSocket streaming** — market data and fills are polled via REST. The adapter boundary is clean for future WebSocket integration.
2. **Live fill detection via remaining_size delta** — the SDK does not expose a fills/trades endpoint, so fills are inferred by comparing the exchange's `remaining_size` against local records each cycle. This is reliable but not instant.
3. **py-clob-client SDK** — live mode depends on the official SDK (`py-clob-client`). The adapter normalizes multiple SDK response field naming conventions. If the SDK changes its response shape significantly, `_normalize_exchange_order` may need updating.
4. **No native post-only order type** — the SDK does not expose a post-only flag. Safety is enforced by checking top-of-book before submission and refusing/repricing orders that would cross.
5. **No historical backtest** — this is a forward-looking scanner, not a backtester.

## License

For personal use. Not financial advice. Trade at your own risk.
