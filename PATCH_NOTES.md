# Patch notes

This patch fixes the GitHub Actions failure:

```text
KeyError: 'symbol'
```

Root cause: the GitHub runner installed pandas 3.x. The original `add_indicators()` function used `groupby(...).apply(...)`, and under the runner's pandas behavior the grouping column `symbol` was no longer available after the apply step. The next VWAP grouping then failed.

Fixes:

1. Replaced the two `groupby.apply(...)` sections in `daytrading/indicators.py` with explicit per-symbol loops and `pd.concat(...)`.
2. Added input-frame validation so future provider/schema issues fail with a clearer message.
3. Pinned `pandas` to `<3.0.0` in `requirements.txt` for stability.
4. Pinned `numpy` and `yfinance` upper bounds to reduce cloud-environment drift.

Upload the patched files to the repository root, especially:

```text
daytrading/indicators.py
requirements.txt
```
