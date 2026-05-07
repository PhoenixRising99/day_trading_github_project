# Completed-bar paper scan patch

## What happened

The GitHub workflow successfully waited and scanned inside the 10:15-10:59 AM ET
entry window. The uploaded CSVs prove that scans ran at:

- 10:15
- 10:20
- 10:25
- 10:30
- 10:35
- 10:40
- 10:45
- 10:50
- 10:55

However, every symbol had:

- `signal = False`
- `raw_signal_before_time_filter = False`
- `setup_score = 0`
- `reason = missing_indicator`
- `volume_ratio = 0.0`

This usually means the scanner was evaluating the just-opened/current 5-minute
bar from yfinance. That current bar can have zero volume or a zero high/low
range, which makes some indicators invalid.

## Patch

`daytrading/paper.py` now selects the latest completed 5-minute candle for each
symbol instead of blindly using the newest row returned by yfinance.

For example, when the scan runs at 10:20:02 ET, it evaluates the 10:15 candle
instead of the just-opened 10:20 candle.

## Important

This does not force trades. It only prevents incomplete live bars from causing
false `missing_indicator` rejections. If the strategy still finds no signal after
this patch, that is a legitimate no-signal result.
