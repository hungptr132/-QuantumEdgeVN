"""The only module that converts between UTC storage and Vietnamese business dates.

Everything is stored `timestamptz` in UTC, deliberately, so a developer's machine
timezone can never change a computed figure. But a reader in Ho Chi Minh City needs the
local date, and the gap is seven hours wide: a headline filed at 23:30 UTC belongs to the
*next* calendar day in Vietnam. Formatting stored instants in UTC would therefore date
every evening item a day early, on every page.

Invariant 8 says no calc function may ask the clock. This module may, and it is the only
one that may — which is why it is this small and why `today()` lives here rather than
wherever it happened to be needed.

The zone name itself stays in `config.py`, so it appears exactly once in the codebase and
a deployment elsewhere is a settings change rather than a code change.
"""

from datetime import UTC, date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from quantumedge.config import load_settings

# Vietnamese convention: day first, dot-free. Matches how dates read on the ticker page
# and in every Vietnamese filing.
DISPLAY_DATE_FORMAT = "%d/%m/%Y"


@lru_cache(maxsize=1)
def business_tz() -> ZoneInfo:
    """The exchange's timezone, from settings. Cached: `ZoneInfo` reads the tz database."""
    return ZoneInfo(load_settings().timezone)


def to_business_moment(moment: datetime) -> datetime:
    """Re-express an instant in exchange-local time.

    A naive datetime is treated as UTC rather than as local, because every naive value in
    this system came out of Postgres where storage is UTC. Guessing "local" would shift
    stored data by seven hours and nothing would report it.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(business_tz())


def to_business_date(moment: datetime) -> date:
    """The calendar date this instant falls on in Vietnam."""
    return to_business_moment(moment).date()


def format_business_date(moment: datetime) -> str:
    """Preformatted for display, because the web layer holds no formatting logic."""
    return to_business_moment(moment).strftime(DISPLAY_DATE_FORMAT)


def today() -> date:
    """The current exchange-local date.

    The one sanctioned reading of the clock. Calc never calls this: `as_of_date` is always
    passed in explicitly, so that the same inputs reproduce the same conclusion tomorrow.
    """
    return to_business_date(datetime.now(UTC))
