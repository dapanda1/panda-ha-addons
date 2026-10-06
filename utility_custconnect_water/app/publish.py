"""Sensor publishing through the Home Assistant REST API (POST /api/states/<entity_id>)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from typing import Any

import ha

_LOGGER = logging.getLogger("custconnect.publish")

DOMAIN = "utility_custconnect_water"


@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str
    icon: str
    unit: str | None = "gal"
    device_class: str | None = "water"
    state_class: str | None = None


SENSORS: tuple[SensorDef, ...] = (
    SensorDef("last_hour_usage", "Last Hour Usage", "mdi:water"),
    SensorDef("last_day_usage", "Last Full Day Usage", "mdi:water"),
    SensorDef("billing_period_usage", "Billing Period Usage", "mdi:water-pump", state_class="total_increasing"),
    SensorDef("billing_period_projected", "Billing Period Projected", "mdi:chart-line"),
    SensorDef("billing_period_last_year", "Same Period Last Year", "mdi:calendar-clock"),
    SensorDef("data_through", "Data Through", "mdi:clock-check-outline", unit=None, device_class="timestamp"),
)
BILLING_KEYS = ("billing_period_usage", "billing_period_projected", "billing_period_last_year")


class Publisher:
    """Post sensor states; keeps the last values so they can be re-posted after an HA restart."""

    def __init__(self, site_id: str, *, exclude: Iterable[str] = ()) -> None:
        self.prefix = f"sensor.{DOMAIN}_{site_id}"
        excluded = set(exclude)
        self.sensors = tuple(s for s in SENSORS if s.key not in excluded)
        self._last: tuple[dict[str, Any], dict[str, dict[str, Any]]] | None = None
        self._last_status: tuple[str, dict[str, Any]] | None = None

    def entity_id(self, key: str) -> str:
        return f"{self.prefix}_{key}"

    async def publish(self, values: dict[str, Any], attributes: dict[str, dict[str, Any]]) -> None:
        """Post all enabled sensors; missing values post as unknown."""
        self._last = (values, attributes)
        await self._post(values, attributes)

    async def post_status(self, status: str, attributes: dict[str, Any]) -> None:
        """Post the add-on status sensor (ok / error / auth_failed / starting)."""
        self._last_status = (status, attributes)
        await ha.set_state(
            self.entity_id("status"),
            status,
            {"friendly_name": "CustConnect Water Status", **attributes},
        )

    async def republish(self) -> bool:
        """Re-post the last values and status (used after Home Assistant restarts).

        Returns:
            True when there was something to post.
        """
        posted = False
        if self._last is not None:
            await self._post(*self._last)
            posted = True
        if self._last_status is not None:
            await self.post_status(*self._last_status)
            posted = True
        return posted

    async def _post(self, values: dict[str, Any], attributes: dict[str, dict[str, Any]]) -> None:
        for s in self.sensors:
            attrs: dict[str, Any] = {
                "friendly_name": f"CustConnect Water {s.name}",
                "icon": s.icon,
                **attributes.get(s.key, {}),
            }
            if s.unit:
                attrs["unit_of_measurement"] = s.unit
            if s.device_class:
                attrs["device_class"] = s.device_class
            if s.state_class:
                attrs["state_class"] = s.state_class
            value = values.get(s.key)
            await ha.set_state(self.entity_id(s.key), "unknown" if value is None else value, attrs)
