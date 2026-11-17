"""Alert on unsuccessful paper-trading Actions runs; no broker operations."""
import os

from daytrading.diagnostics import _alert


def main() -> int:
    result = os.environ.get("SOURCE_CONCLUSION", "unknown")
    run_id = os.environ.get("SOURCE_RUN_ID", "?")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    _alert(f"Paper trading GitHub Actions run {run_id} completed with "
           f"{result}. Review https://github.com/{repo}/actions/runs/{run_id}")
    # Alerting job itself is successful if warning was generated.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
