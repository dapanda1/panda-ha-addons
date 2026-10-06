"""Track when the portal publishes new readings.

The portal adds readings in batches. Each time an update sees `data_through` move forward,
the time it was first seen is recorded (persisted in /data) and exposed on the
`data_through` sensor, so the publishing schedule can be read from Home Assistant.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

_LOGGER = logging.getLogger("custconnect.arrivals")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
KEEP = 14


class ArrivalLog:
    def __init__(self, site_id: str) -> None:
        self._path = os.path.join(DATA_DIR, f"arrivals_{site_id}.json")
        self.entries: list[dict[str, str]] = self._load()

    def _load(self) -> list[dict[str, str]]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            return [e for e in data if isinstance(e, dict) and "data_through" in e][-KEEP:]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh)
        except OSError as err:
            _LOGGER.warning("Could not save arrival log: %s", err)

    def observe(self, data_through: str | None, tz: dt.tzinfo) -> None:
        """Record the first time a newer `data_through` is seen."""
        if not data_through:
            return
        last = self.entries[-1]["data_through"] if self.entries else None
        if last is not None and _parse(data_through) <= _parse(last):
            return
        now = dt.datetime.now(tz=dt.UTC).astimezone(tz)
        entry = {"data_through": data_through, "received_at": now.isoformat(timespec="minutes")}
        if last is not None:
            lag = now - _parse(data_through)
            entry["lag_hours"] = f"{lag.total_seconds() / 3600:.1f}"
            _LOGGER.info(
                "New readings: data now through %s (received %s, %s h after the newest reading)",
                _short(data_through, tz), now.strftime("%Y-%m-%d %H:%M"), entry["lag_hours"],
            )
        else:
            # First run: the data was already there; the arrival time is unknown.
            entry["received_at"] = "before first update"
        self.entries = (self.entries + [entry])[-KEEP:]
        self._save()

    def attributes(self) -> dict[str, Any]:
        latest = self.entries[-1] if self.entries else {}
        return {
            "received_at": latest.get("received_at"),
            "lag_hours": latest.get("lag_hours"),
            "recent_arrivals": [
                {k: e[k] for k in ("data_through", "received_at", "lag_hours") if k in e}
                for e in reversed(self.entries)
            ],
        }


def _parse(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def _short(iso: str, tz: dt.tzinfo) -> str:
    return _parse(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")
