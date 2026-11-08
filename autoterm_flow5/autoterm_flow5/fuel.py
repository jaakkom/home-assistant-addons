"""Fuel use from the dosing-pump frequency."""

from __future__ import annotations

import json
from pathlib import Path

# TH-11 / assy 4995: 4.4 ml per 100 strokes.
DEFAULT_DOSE_ML = 0.044
MAX_GAP_S = 5.0
HOUR_S = 3600.0
BUCKET_S = 60.0


def liters_per_hour(frequency_hz: float, dose_ml: float = DEFAULT_DOSE_ML) -> float:
    if frequency_hz <= 0 or dose_ml <= 0:
        return 0.0
    return frequency_hz * dose_ml * 3.6


def milliliters_per_minute(frequency_hz: float, dose_ml: float = DEFAULT_DOSE_ML) -> float:
    return liters_per_hour(frequency_hz, dose_ml) * 1000 / 60


class FuelMeter:
    """Integrate pump strokes into a persisted total and a tank level.

    The frequency applied to an interval is the frequency reported at the
    start of that interval. A gap longer than five seconds adds nothing.
    The tank level stays unknown until a fill or a manual level is stored.
    Remaining time uses the average consumption over the observed seconds
    of the last hour. Idle time in that hour lowers the average.
    """

    def __init__(self, path: Path | None = None, dose_ml: float = DEFAULT_DOSE_ML) -> None:
        self.path = path
        self.dose_ml = dose_ml
        self.total_ml = 0.0
        self.remaining_ml: float | None = None
        self.pending_save = False
        self._buckets: list[list[float]] = []
        self._hour_end: float | None = None
        self._last_time: float | None = None
        self._last_hz = 0.0
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.total_ml = float(data.get("total_ml", 0.0))
                if data.get("remaining_ml") is not None:
                    self.remaining_ml = float(data["remaining_ml"])
                self._buckets = _load_buckets(data.get("hour_buckets", []))
                if data.get("hour_end") is not None:
                    self._hour_end = float(data["hour_end"])
            except (OSError, ValueError, TypeError):
                self.total_ml = 0.0
                self.remaining_ml = None
                self._buckets = []
                self._hour_end = None

    @property
    def total_liters(self) -> float:
        return self.total_ml / 1000

    @property
    def remaining_liters(self) -> float | None:
        if self.remaining_ml is None:
            return None
        return self.remaining_ml / 1000

    def level_percent(self, capacity_l: float) -> float | None:
        if self.remaining_ml is None or capacity_l <= 0:
            return None
        return min(100.0, max(0.0, self.remaining_ml / (capacity_l * 1000) * 100))

    def average_lph(self) -> float:
        """Litres per hour over the observed seconds of the last hour."""
        now = self._last_time if self._last_time is not None else self._hour_end
        if now is None:
            return 0.0
        cutoff = now - HOUR_S
        milliliters = 0.0
        seconds = 0.0
        for start, span, amount in self._buckets:
            if start < cutoff:
                continue
            milliliters += amount
            seconds += span
        if seconds <= 0 or milliliters <= 0:
            return 0.0
        return (milliliters / 1000) * (HOUR_S / seconds)

    def hours_remaining(self) -> float | None:
        """Hours left at the last hour's average consumption."""
        rate = self.average_lph()
        if self.remaining_ml is None or rate <= 0:
            return None
        return (self.remaining_ml / 1000) / rate

    def refuel(self, capacity_l: float) -> bool:
        """Mark the tank full. The lifetime total is left unchanged."""
        if capacity_l <= 0:
            return False
        self.remaining_ml = capacity_l * 1000
        return True

    def set_level_liters(self, liters: float, capacity_l: float) -> None:
        """Store a known amount, for a partial fill."""
        amount = max(0.0, liters)
        if capacity_l > 0:
            amount = min(amount, capacity_l)
        self.remaining_ml = amount * 1000

    def clamp_to_capacity(self, capacity_l: float) -> bool:
        if self.remaining_ml is None or capacity_l <= 0:
            return False
        capped = min(self.remaining_ml, capacity_l * 1000)
        if capped == self.remaining_ml:
            return False
        self.remaining_ml = capped
        return True

    def update(self, now: float, frequency_hz: float, dose_ml: float | None = None) -> float:
        dose = self.dose_ml if dose_ml is None else dose_ml
        self.dose_ml = dose
        added = 0.0
        if self._last_time is not None:
            dt = now - self._last_time
            if 0 < dt <= MAX_GAP_S:
                if self._last_hz > 0 and dose > 0:
                    added = self._last_hz * dose * dt
                    self.total_ml += added
                    if self.remaining_ml is not None:
                        self.remaining_ml = max(0.0, self.remaining_ml - added)
                self._record(now, dt, added)
                self._hour_end = now
        self._last_time = now
        self._last_hz = max(0.0, frequency_hz)
        return added

    def _record(self, end: float, dt: float, milliliters: float) -> None:
        start = float(int(end // BUCKET_S) * int(BUCKET_S))
        if not self._buckets or self._buckets[-1][0] != start:
            self._buckets.append([start, 0.0, 0.0])
            self.pending_save = True
        bucket = self._buckets[-1]
        bucket[1] += dt
        bucket[2] += milliliters
        if milliliters > 0:
            self.pending_save = True
        cutoff = end - HOUR_S
        if self._buckets[0][0] < cutoff:
            self._buckets = [item for item in self._buckets if item[0] >= cutoff]
            self.pending_save = True

    def reset(self) -> None:
        self.total_ml = 0.0
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "total_ml": self.total_ml,
                    "remaining_ml": self.remaining_ml,
                    "hour_buckets": self._buckets,
                    "hour_end": self._hour_end,
                }
            ),
            encoding="utf-8",
        )
        self.pending_save = False


def _load_buckets(raw: object) -> list[list[float]]:
    if not isinstance(raw, list):
        return []
    buckets: list[list[float]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            continue
        try:
            buckets.append([float(item[0]), float(item[1]), float(item[2])])
        except (TypeError, ValueError):
            continue
    return buckets
