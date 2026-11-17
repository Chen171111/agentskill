"""Fail-closed A-share order windows, evaluated in Asia/Shanghai."""
from datetime import datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_WINDOWS = ((time(9, 30), time(11, 30)), (time(13), time(14, 56)))


def _local_now(now=None):
    current = now if now is not None else datetime.now(SHANGHAI)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def require_trading_session(now=None):
    """Return Shanghai time or raise before any real connection/submission.

    Naive injected times mean Shanghai wall time. The calendar is read only
    from the local cache; unavailable or expired calendars never permit trade.
    """
    from dataprovider.calendar import is_trading_day
    current = _local_now(now)
    try:
        trading = is_trading_day(current, local_only=True)
    except Exception as exc:
        raise RuntimeError("本地交易日历不可用，拒绝交易: {}".format(exc)) from exc
    wall = current.time().replace(tzinfo=None)
    if not trading or not any(start <= wall < end for start, end in _WINDOWS):
        raise RuntimeError("不在交易时段，拒绝交易: {}".format(current.isoformat()))
    return current


def current_trading_session_deadline(now=None):
    """Return the end of the current morning/afternoon order window."""
    current = require_trading_session(now)
    wall = current.time().replace(tzinfo=None)
    for start, end in _WINDOWS:
        if start <= wall < end:
            return datetime.combine(current.date(), end, tzinfo=SHANGHAI)
    raise RuntimeError("不在交易时段")
