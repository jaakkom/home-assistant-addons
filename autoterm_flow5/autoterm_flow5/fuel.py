"""Fuel use from the dosing-pump frequency."""

from __future__ import annotations

import json
from pathlib import Path

# TH-11 / assy 4995: 4.4 ml per 100 strokes.
DEFAULT_DOSE_ML = 0.044
MAX_GAP_S = 5.0


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
    """

    def __init__(self, path: Path | None = None, dose_ml: float = DEFAULT_DOSE_ML) -> None:
        self.path = path
        self.dose_ml = dose_ml
        self.total_ml = 0.0
        self.remaining_ml: float | None = None
        self.last_burn_lph = 0.0
        self._last_time: float | None = None
        self._last_hz = 0.0
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.total_ml = float(data.get("total_ml", 0.0))
                if data.get("remaining_ml") is not None:
                    self.remaining_ml = float(data["remaining_ml"])
                self.last_burn_lph = float(data.get("last_burn_lph", 0.0))
            except (OSError, ValueError, TypeError):
                self.total_ml = 0.0
                self.remaining_ml = None
                self.last_burn_lph = 0.0

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

    def hours_remaining(self) -> float | None:
        """Hours left at the latest non-zero burn rate."""
        if self.remaining_ml is None or self.last_burn_lph <= 0:
            return None
        return (self.remaining_ml / 1000) / self.last_burn_lph

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
            if 0 < dt <= MAX_GAP_S and self._last_hz > 0 and dose > 0:
                added = self._last_hz * dose * dt
                self.total_ml += added
                if self.remaining_ml is not None:
                    self.remaining_ml = max(0.0, self.remaining_ml - added)
        self._last_time = now
        self._last_hz = max(0.0, frequency_hz)
        if self._last_hz > 0 and dose > 0:
            self.last_burn_lph = liters_per_hour(self._last_hz, dose)
        return added

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
                    "last_burn_lph": self.last_burn_lph,
                }
            ),
            encoding="utf-8",
        )
