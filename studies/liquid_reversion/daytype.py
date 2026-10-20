"""Range-day classification from the first 30 minutes and completed prior sessions.

Two features, each measured on the day itself and scaled by its median over
earlier sessions:

* ``drive = |close at 10:00 / session open - 1|``, the opening drive;
* ``gap = |session open / prior close - 1|``, the overnight gap.

Only bars that have ended by 10:00 New York are read from the day, so the
classification is known at 10:00, before the first possible signal is acted on.
A day is a range day when both ratios are below their thresholds. With fewer
than ``min_history`` earlier values the day is unknown and is not traded.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
from statistics import median

from studies.liquid_reversion.bars import REGULAR_OPEN, Bar, ny_instant

CLASSIFY_AT = time(10, 0)
LOOKBACK = 20
MIN_HISTORY = 10
DRIVE_GRID = (0.6, 0.8, 1.0)
GAP_GRID = (1.0, 1.5, math.inf)


@dataclass(frozen=True, slots=True)
class OpeningFeatures:
    """What the day shows by 10:00: the open, the 10:00 close, drive and gap."""

    session_open: float | None
    close_at_cutoff: float | None
    drive: float | None
    gap: float | None


@dataclass(frozen=True, slots=True)
class DayType:
    """Features and ratios for one symbol-session; ratios are ``None`` when unknown."""

    session: date
    drive: float | None
    gap: float | None
    drive_ratio: float | None
    gap_ratio: float | None

    @property
    def known(self) -> bool:
        return self.drive_ratio is not None and self.gap_ratio is not None


def is_range_day(day: DayType, k_drive: float, k_gap: float) -> bool:
    """A known day whose drive and gap ratios are both below their thresholds."""
    return (day.drive_ratio is not None and day.gap_ratio is not None
            and day.drive_ratio < k_drive and day.gap_ratio < k_gap)


def opening_features(day: date, bars: Sequence[Bar], prior_close: float | None) -> OpeningFeatures:
    """Features from the bars that ended by 10:00 New York, plus the prior close.

    The session open is the open of the 09:30 bar; without that bar the day
    has no features. Later bars are never read.
    """
    opening = ny_instant(day, REGULAR_OPEN)
    cutoff = ny_instant(day, CLASSIFY_AT)
    usable: list[Bar] = []
    for bar in bars:
        if bar.end > cutoff:
            break
        usable.append(bar)
    if not usable or usable[0].ts != opening:
        return OpeningFeatures(None, None, None, None)
    session_open = usable[0].open
    close_at_cutoff = usable[-1].close
    gap = abs(session_open / prior_close - 1.0) if prior_close else None
    return OpeningFeatures(session_open, close_at_cutoff, abs(close_at_cutoff / session_open - 1.0), gap)


class DayTypeTracker:
    """Causal rolling medians of the drive and the gap over earlier sessions."""

    def __init__(self, *, lookback: int = LOOKBACK, min_history: int = MIN_HISTORY) -> None:
        if not 1 <= min_history <= lookback:
            raise ValueError("need 1 <= min_history <= lookback")
        self.min_history = min_history
        self._drives: deque[float] = deque(maxlen=lookback)
        self._gaps: deque[float] = deque(maxlen=lookback)

    def classify(self, day: date, bars: Sequence[Bar], prior_close: float | None) -> DayType:
        """Classify ``day`` against earlier sessions only, then remember its features."""
        features = opening_features(day, bars, prior_close)
        result = DayType(day, features.drive, features.gap,
                         self._ratio(features.drive, self._drives),
                         self._ratio(features.gap, self._gaps))
        if features.drive is not None:
            self._drives.append(features.drive)
        if features.gap is not None:
            self._gaps.append(features.gap)
        return result

    def _ratio(self, value: float | None, history: deque[float]) -> float | None:
        if value is None or len(history) < self.min_history:
            return None
        scale = median(history)
        return value / scale if scale > 0 else None


def classify_sessions(sessions: Mapping[date, Sequence[Bar]], *, lookback: int = LOOKBACK,
                      min_history: int = MIN_HISTORY) -> dict[date, DayType]:
    """Day types for one symbol's sessions, each judged only on what came before it."""
    tracker = DayTypeTracker(lookback=lookback, min_history=min_history)
    prior_close: float | None = None
    out: dict[date, DayType] = {}
    for day in sorted(sessions):
        bars = sessions[day]
        if not bars:
            continue
        out[day] = tracker.classify(day, bars, prior_close)
        prior_close = bars[-1].close
    return out
