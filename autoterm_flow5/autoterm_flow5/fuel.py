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
    """Integrate pump strokes into a persisted total.

    The frequency applied to an interval is the frequency reported at the
    start of that interval. A gap longer than five seconds adds nothing.
    """

    def __init__(self, path: Path | None = None, dose_ml: float = DEFAULT_DOSE_ML) -> None:
        self.path = path
        self.dose_ml = dose_ml
        self.total_ml = 0.0
        self._last_time: float | None = None
        self._last_hz = 0.0
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.total_ml = float(data.get("total_ml", 0.0))
            except (OSError, ValueError, TypeError):
                self.total_ml = 0.0

    @property
    def total_liters(self) -> float:
        return self.total_ml / 1000

    def update(self, now: float, frequency_hz: float, dose_ml: float | None = None) -> float:
        dose = self.dose_ml if dose_ml is None else dose_ml
        self.dose_ml = dose
        added = 0.0
        if self._last_time is not None:
            dt = now - self._last_time
            if 0 < dt <= MAX_GAP_S and self._last_hz > 0 and dose > 0:
                added = self._last_hz * dose * dt
                self.total_ml += added
        self._last_time = now
        self._last_hz = max(0.0, frequency_hz)
        return added

    def reset(self) -> None:
        self.total_ml = 0.0
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"total_ml": self.total_ml}),
            encoding="utf-8",
        )
