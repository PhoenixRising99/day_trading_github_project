"""Archive the exact market-data/indicator frame seen by a paper decision.

Artifacts only (not committed); enable in the trading workflow. Archival
failure is visible but must not interrupt monitoring or order reconciliation.
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "logs" / "market_snapshots"


def capture_frame(frame, stage: str, *, directory: Path = ROOT) -> None:
    if os.environ.get("PAPER_CAPTURE_MARKET_DATA") != "1":
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        tag = f"{stage}-{now}-{uuid.uuid4().hex[:8]}"
        target = directory / (tag + ".csv.gz")
        temporary = directory / (tag + ".tmp")
        # Retain full warmup, input OHLCV, incomplete bars, and computed
        # indicators, with round-trip floating point precision.
        frame.to_csv(temporary, index=False, compression="gzip", float_format="%.17g")
        temporary.replace(target)
        print(f"MARKET_SNAPSHOT={target.name} rows={len(frame)}", flush=True)
    except Exception as exc:
        print(f"::warning::Market snapshot archival failed ({type(exc).__name__}).", flush=True)
