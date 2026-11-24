"""Exchange calendar for audit jobs. Read-only; never invokes order endpoints."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.requests import GetCalendarRequest
from daytrading.broker import AlpacaPaperBroker

ET = ZoneInfo("America/New_York")
LOGGING_START = date(2026, 11, 17)


def completed_sessions(start: date, end: date, *, now=None):
    now = now or datetime.now(ET)
    client = AlpacaPaperBroker.from_env().client()
    calendar = client.get_calendar(filters=GetCalendarRequest(start=start, end=end))
    result = {}
    for session in calendar:
        day = session.date
        if not isinstance(day, date):
            day = date.fromisoformat(str(day))
        close = session.close
        if isinstance(close, datetime):
            close = close.replace(tzinfo=ET) if close.tzinfo is None else close.astimezone(ET)
        else:
            from datetime import time
            if not isinstance(close, time):
                close = time.fromisoformat(str(close))
            close = datetime.combine(day, close, ET)
        if close + timedelta(minutes=15) <= now:
            result[day.isoformat()] = close.isoformat()
    return result
