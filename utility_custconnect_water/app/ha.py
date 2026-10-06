"""Home Assistant Core access through the Supervisor proxy.

- WebSocket: get_config, recorder/statistics_during_period, recorder/import_statistics,
  recorder/clear_statistics, subscribe_events (homeassistant_started)
- REST: /api/states (sensor publishing), /api/services (notifications)

Development overrides (not set inside the add-on): HA_URL replaces the Supervisor
proxy URL and HA_TOKEN replaces SUPERVISOR_TOKEN, for running against a local instance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
import datetime as dt
import itertools
import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger("custconnect.ha")

SUPERVISOR_URL = os.environ.get("HA_URL", "http://supervisor/core")
IMPORT_CHUNK = 2000


class HAError(Exception):
    """Home Assistant API error."""


def _token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN")
    if not token:
        raise HAError("SUPERVISOR_TOKEN not set (homeassistant_api must be enabled)")
    return token


class HAWebSocket:
    """Minimal authenticated WebSocket client."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> HAWebSocket:
        url = SUPERVISOR_URL.replace("http", "ws", 1) + "/websocket"
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url, max_msg_size=0, heartbeat=30)
            hello = await self._receive()
            if hello.get("type") != "auth_required":
                raise HAError(f"unexpected greeting: {hello.get('type')}")
            await self._ws.send_json({"type": "auth", "access_token": _token()})
            result = await self._receive()
            if result.get("type") != "auth_ok":
                raise HAError(f"websocket auth failed: {result.get('message', result.get('type'))}")
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _receive(self) -> dict[str, Any]:
        """Next JSON message.

        Raises:
            HAError: The connection closed (e.g. Home Assistant restarting).
        """
        assert self._ws
        msg = await self._ws.receive()
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            raise HAError("websocket closed")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise HAError(f"websocket error: {self._ws.exception()}")
        return msg.json()

    async def events(self, event_type: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to an event type and yield events until the connection closes."""
        assert self._ws
        sub_id = next(self._ids)
        await self._ws.send_json({"id": sub_id, "type": "subscribe_events", "event_type": event_type})
        while True:
            msg = await self._receive()
            if msg.get("id") != sub_id:
                continue
            if msg.get("type") == "result" and not msg.get("success"):
                raise HAError(f"subscribe_events failed: {msg.get('error')}")
            if msg.get("type") == "event":
                yield msg.get("event") or {}

    async def call(self, msg_type: str, **payload: Any) -> Any:
        """Send a command and wait for its result.

        Returns:
            The ``result`` field.

        Raises:
            HAError: The command failed.
        """
        assert self._ws
        msg_id = next(self._ids)
        await self._ws.send_json({"id": msg_id, "type": msg_type, **payload})
        while True:
            msg = await self._receive()
            if msg.get("id") != msg_id:
                continue
            if msg.get("type") == "result":
                if not msg.get("success"):
                    err = msg.get("error") or {}
                    raise HAError(f"{msg_type}: {err.get('code')}: {err.get('message')}")
                return msg.get("result")


def _to_dt(value: Any) -> dt.datetime:
    """Statistics rows report ``start`` as epoch ms (newer HA) or ISO text (older)."""
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC)
    return dt.datetime.fromisoformat(str(value)).astimezone(dt.UTC)


class Statistics:
    """External statistic series for one meter."""

    # Index of the metadata variant the running HA version accepted; reused after the first import.
    _accepted_variant: int | None = None

    def __init__(self, ws: HAWebSocket, statistic_id: str, name: str) -> None:
        self._ws = ws
        self.statistic_id = statistic_id
        self.name = name
        self.source = statistic_id.split(":", 1)[0]

    async def rows(self, start: dt.datetime, end: dt.datetime, period: str = "hour") -> list[dict[str, Any]]:
        """Existing rows in [start, end).

        Returns:
            Rows with ``start`` as aware UTC datetimes.
        """
        result = await self._ws.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=[self.statistic_id],
            period=period,
            types=["sum"],
        )
        rows = (result or {}).get(self.statistic_id, [])
        return [{**r, "start": _to_dt(r["start"])} for r in rows]

    async def last_hour(self, now: dt.datetime, lookback_days: int = 800) -> dt.datetime | None:
        """Start of the most recent recorded hour.

        Returns:
            Aware UTC datetime, or None when the series is empty.
        """
        months = await self.rows(now - dt.timedelta(days=lookback_days), now, period="month")
        if not months:
            return None
        month_start = max(r["start"] for r in months)
        hours = await self.rows(month_start - dt.timedelta(days=1), now)
        return max(r["start"] for r in hours) if hours else None

    async def anchor(self, before: dt.datetime) -> tuple[dt.datetime | None, float]:
        """Most recent row strictly before ``before``; the fold continues from its sum.

        Returns:
            (row start, row sum) or (None, 0.0) for a cold start.
        """
        rows = [r for r in await self.rows(before - dt.timedelta(days=14), before) if r["start"] < before]
        if not rows:
            return None, 0.0
        row = max(rows, key=lambda r: r["start"])
        return row["start"], float(row.get("sum") or 0.0)

    async def import_rows(self, rows: list[dict[str, Any]]) -> None:
        """Upsert rows (``start`` aware datetime, ``state``, ``sum``).

        Raises:
            HAError: Every metadata variant was rejected.
        """
        base = {
            "has_sum": True,
            "name": self.name,
            "source": self.source,
            "statistic_id": self.statistic_id,
            "unit_of_measurement": "gal",
        }
        # Metadata keys differ across HA versions; newest first.
        variants = [
            {**base, "mean_type": 0, "unit_class": "volume"},
            {**base, "mean_type": 0},
            {**base, "has_mean": False},
        ]
        payload = [
            {"start": r["start"].isoformat(), "state": r["state"], "sum": r["sum"]} for r in rows
        ]
        order = list(range(len(variants)))
        if Statistics._accepted_variant is not None:
            order.remove(Statistics._accepted_variant)
            order.insert(0, Statistics._accepted_variant)
        last_error: HAError | None = None
        for index in order:
            metadata = variants[index]
            try:
                for i in range(0, len(payload), IMPORT_CHUNK):
                    await self._ws.call(
                        "recorder/import_statistics",
                        metadata=metadata,
                        stats=payload[i : i + IMPORT_CHUNK],
                    )
            except HAError as err:
                last_error = err
                _LOGGER.debug("import_statistics rejected metadata %s: %s", sorted(metadata), err)
                continue
            Statistics._accepted_variant = index
            return
        raise last_error or HAError("import_statistics failed")


def fold(
    buckets: Iterable[tuple[dt.datetime, float]],
    anchor_start: dt.datetime | None,
    anchor_sum: float,
) -> list[dict[str, Any]]:
    """Accumulate per-hour gallons after the anchor into cumulative rows.

    Returns:
        Rows with ``start``, ``state`` (hour gallons) and ``sum`` (running total).
    """
    out: list[dict[str, Any]] = []
    running = anchor_sum
    for start, gallons in buckets:
        if anchor_start is not None and start <= anchor_start:
            continue
        running += gallons
        out.append({"start": start, "state": gallons, "sum": round(running, 3)})
    return out


async def set_state(entity_id: str, state: Any, attributes: dict[str, Any]) -> None:
    """Create or update an entity state through the REST API (not persisted across HA restarts)."""
    async with aiohttp.ClientSession() as session, session.post(
        f"{SUPERVISOR_URL}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {_token()}"},
        json={"state": state, "attributes": attributes},
    ) as resp:
        if resp.status >= 400:
            raise HAError(f"POST states/{entity_id}: HTTP {resp.status}")


async def call_service(domain: str, service: str, data: dict[str, Any]) -> None:
    """Call a Home Assistant service through the REST API.

    Raises:
        HAError: The call failed.
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session, session.post(
            f"{SUPERVISOR_URL}/api/services/{domain}/{service}",
            headers={"Authorization": f"Bearer {_token()}"},
            json=data,
        ) as resp:
            if resp.status >= 400:
                raise HAError(f"{domain}.{service}: HTTP {resp.status}")
    except aiohttp.ClientError as err:
        raise HAError(f"{domain}.{service}: {err}") from err
