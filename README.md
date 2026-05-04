# Day-Trading Paper Observation Scanner

This is the GitHub Actions version of the frozen V9/V10/V11 paper-observation strategy.

It is designed for:

- Cloud-based scheduled paper scans
- Signal logging
- Manual review
- Forward observation

It is **not** designed for:

- Live trading
- Brokerage login
- Automatic order submission
- SoFi account control

No brokerage credentials are used anywhere in this project.

## Strategy status

The strategy rules are intentionally frozen from the V9/V10/V11 notebook version:

- Watchlist: `SPY`, `AAPL`, `MSFT`
- Timeframe: 5-minute candles
- Entry window: 10:15 AM - 10:59 AM Eastern
- Setup type: morning VWAP-hold continuation
- Paper only
- Max one trade per day in the original backtest logic
- Manual review required

## Folder structure

```text
.
├─ .github/
│  └─ workflows/
│     └─ paper_scan.yml
├─ daytrading/
│  ├─ __init__.py
│  ├─ config.py
│  ├─ data_fetch.py
│  ├─ indicators.py
│  ├─ paper.py
│  └─ strategy.py
├─ data/
│  └─ logs/
│     ├─ scans/
│     └─ paper_trading/
│        └─ paper_trade_journal.csv
├─ paper_scan_job.py
├─ requirements.txt
└─ README.md
```

## How the GitHub schedule works

The workflow runs every 5 minutes during the strategy's entry window:

```yaml
schedule:
  - cron: "16-56/5 10 * * 1-5"
    timezone: "America/New_York"
```

That means roughly:

```text
10:16, 10:21, 10:26, 10:31, 10:36, 10:41, 10:46, 10:51, 10:56 AM ET
```

For Washington/Pacific time, that is usually:

```text
7:16, 7:21, 7:26, 7:31, 7:36, 7:41, 7:46, 7:51, 7:56 AM PT
```

The script itself still checks the timestamp of the latest market-data bar and only marks a signal active if that data bar is inside the strategy's entry window.

## Setup steps

### 1. Create a GitHub repository

Create a new repo on GitHub. A private repo is fine.

### 2. Upload this project

From the unzipped project folder:

```bash
git init
git add .
git commit -m "Initial paper observation scanner"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git push -u origin main
```

### 3. Confirm the workflow appears

Go to:

```text
GitHub repo → Actions → Paper Observation Scan
```

You can click **Run workflow** to test it manually.

### 4. Optional: add Discord alerts

To receive phone notifications only when an active paper signal appears:

1. Create a Discord webhook in a private Discord channel.
2. In GitHub, go to: `Settings → Secrets and variables → Actions`.
3. Add a repository secret named:

```text
DISCORD_WEBHOOK_URL
```

The scanner will still work without this secret. Without it, results are saved as GitHub artifacts and journal rows only.

## Outputs

Every run writes scan output under:

```text
data/logs/scans/
```

GitHub uploads these as workflow artifacts for 14 days.

If an active signal appears, it is appended to:

```text
data/logs/paper_trading/paper_trade_journal.csv
```

The workflow commits the journal back to the repository only when the journal changes.

## Manual local run

You can also run the scanner locally:

```bash
pip install -r requirements.txt
python paper_scan_job.py --period 5d --notes "local test"
```

## Important limitations

- yfinance/free market data can be delayed, incomplete, or rate-limited.
- GitHub scheduled jobs can be delayed by runner availability.
- This is suitable for paper-observation research, not live trade execution.
- No order should be submitted without manual review.

## Paper-observation rule

Do not tune the strategy again until you have at least:

- 20 completed paper trades, or
- 20 trading days of forward observation,

whichever takes longer.
