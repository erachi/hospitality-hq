"""In-stay window predicate.

Stay channels are provisioned for a wide window (today-3d .. today+30d)
so the ambient agent and the journal are ready long before and after
check-in. But not every provisioned reservation is one where the guest
is actually at the property right now — pre-booked future reservations
sit in the same channel pool.

`is_in_stay_window` is the predicate the auto-draft path uses to decide
whether to fire a draft reply on an inbound guest message. We include
the day before check-in so early-arrival logistics (ETA, bag drop,
code requests) still get drafts.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional


_EARLY_ARRIVAL_DAYS = 1


def _parse(d: str) -> Optional[date]:
    """Parse a `YYYY-MM-DD` or ISO-8601 string into a `date`, or None."""
    if not d:
        return None
    # Hospitable/StayChannels usually store `YYYY-MM-DD` but the full
    # ISO form sneaks in via reservation snapshots. Normalize by
    # stripping the time component.
    head = d.split("T", 1)[0]
    try:
        return date.fromisoformat(head)
    except (ValueError, TypeError):
        return None


def is_in_stay_window(
    check_in: str,
    check_out: str,
    today: Optional[date] = None,
) -> bool:
    """Return True iff today is between (check_in - 1d) and check_out.

    - Both dates are inclusive on their own end.
    - A malformed date string returns False (fail closed — no draft).
    - `today` defaults to the current UTC date. Scottsdale is UTC-7,
      so the UTC date can be up to 7h ahead of local; the one-day
      early-arrival pad absorbs that drift without a timezone import.
    """
    ci = _parse(check_in)
    co = _parse(check_out)
    if ci is None or co is None:
        return False
    today = today or datetime.now(timezone.utc).date()
    return (ci - timedelta(days=_EARLY_ARRIVAL_DAYS)) <= today <= co
