"""Utility CustConnect Water add-on entry point."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import datetime as dt
import json
import logging
import os
import signal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from alerts import AlertConfig, Alerter, normalize_service, notify_config_error
from arrivals import ArrivalLog
from client import AuthError, BillingProgress, HourRecord, Portal, PortalClient, PortalError, parse_portal
import ha
from publish import BILLING_KEYS, DOMAIN, Publisher

# OPTIONS_PATH is a development override (not set inside the add-on) for running
# against a local options file.
OPTIONS_PATH = os.environ.get("OPTIONS_PATH", "/data/options.json")
MAX_BACKFILL_DAYS = 400

_LOGGER = logging.getLogger("custconnect")


@dataclass(frozen=True)
class Options:
    """Validated add-on options (all set from the add-on Configuration tab)."""

    portal: Portal
    username: str
    password: str
    time_zone: str | None
    poll_interval: dt.timedelta
    retry_interval: dt.timedelta
    auth_retry_interval: dt.timedelta
    initial_history_days: int
    refresh_days: int
    clear_statistics_on_start: bool
    statistic_name: str | None
    billing_sensors: bool
    notify_on_error: bool
    notify_after_failures: int
    notify_on_recovery: bool
    notify_service: str | None
    notify_high_priority: bool
    log_level: str


def load_options(path: str = OPTIONS_PATH) -> Options:
    """Read /data/options.json written by the Supervisor.

    Returns:
        Parsed options with defaults for omitted optional keys.

    Raises:
        SystemExit: A required option is missing or invalid.
    """
    with open(path, encoding="utf-8") as fh:
        raw: dict[str, Any] = json.load(fh)

    def text(key: str) -> str | None:
        value = raw.get(key)
        return str(value).strip() if value not in (None, "") and str(value).strip() else None

    username = text("username")
    password = raw.get("password") or ""
    missing = [k for k, v in (("portal_url", text("portal_url")), ("username", username), ("password", password)) if not v]
    if missing:
        raise SystemExit(f"Required option(s) not set: {', '.join(missing)}")
    try:
        portal = parse_portal(text("portal_url") or "")
    except ValueError as err:
        raise SystemExit(f"Option 'portal_url' is invalid: {err}") from err

    try:
        notify_service = normalize_service(text("notify_service"))
    except ValueError as err:
        raise SystemExit(f"Option 'notify_service' is invalid: {err}") from err

    time_zone = text("time_zone")
    if time_zone:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise SystemExit(f"Option 'time_zone' is not a valid IANA zone: {time_zone}") from err

    return Options(
        portal=portal,
        username=username or "",
        password=password,
        time_zone=time_zone,
        poll_interval=dt.timedelta(minutes=int(raw.get("poll_interval_minutes", 60))),
        retry_interval=dt.timedelta(minutes=int(raw.get("retry_minutes", 15))),
        auth_retry_interval=dt.timedelta(minutes=int(raw.get("auth_retry_minutes", 60))),
        initial_history_days=min(int(raw.get("initial_history_days", 30)), MAX_BACKFILL_DAYS),
        refresh_days=int(raw.get("refresh_days", 7)),
        clear_statistics_on_start=bool(raw.get("clear_statistics_on_start", False)),
        statistic_name=text("statistic_name"),
        billing_sensors=bool(raw.get("billing_sensors", True)),
        notify_on_error=bool(raw.get("notify_on_error", True)),
        notify_after_failures=int(raw.get("notify_after_failures", 3)),
        notify_on_recovery=bool(raw.get("notify_on_recovery", True)),
        notify_service=notify_service,
        notify_high_priority=bool(raw.get("notify_high_priority", True)),
        log_level=str(raw.get("log_level", "info")),
    )


class Poller:
    """One update cycle: portal -> statistics -> sensors."""

    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self.statistic_id = f"{DOMAIN}:{opts.portal.site_id}_water_usage"
        excluded = set() if opts.billing_sensors else set(BILLING_KEYS)
        self.publisher = Publisher(opts.portal.site_id, exclude=excluded)
        self.alerter = Alerter(
            AlertConfig(
                enabled=opts.notify_on_error,
                after_failures=opts.notify_after_failures,
                on_recovery=opts.notify_on_recovery,
                notify_service=opts.notify_service,
                high_priority=opts.notify_high_priority,
                retry_minutes=int(min(opts.poll_interval, opts.retry_interval).total_seconds() // 60),
                auth_retry_minutes=int(opts.auth_retry_interval.total_seconds() // 60),
            ),
            site_id=opts.portal.site_id,
            portal_host=opts.portal.hostname,
        )
        if opts.time_zone:
            self.alerter.tz = ZoneInfo(opts.time_zone)
        self.arrivals = ArrivalLog(opts.portal.site_id)
        self._cleared = False

    async def cycle(self) -> None:
        opts = self.opts
        async with ha.HAWebSocket() as ws:
            if opts.time_zone:
                tz = ZoneInfo(opts.time_zone)
            else:
                config = await ws.call("get_config")
                tz = ZoneInfo(config.get("time_zone") or "UTC")
            self.alerter.tz = tz
            stats = ha.Statistics(
                ws, self.statistic_id, opts.statistic_name or f"Water usage ({opts.portal.hostname})"
            )

            cleared_now = False
            if opts.clear_statistics_on_start and not self._cleared:
                await ws.call("recorder/clear_statistics", statistic_ids=[self.statistic_id])
                cleared_now = True
                _LOGGER.warning(
                    "Cleared statistic %s. Turn off 'clear_statistics_on_start' to keep history on the next restart.",
                    self.statistic_id,
                )
            self._cleared = True

            now = dt.datetime.now(tz=dt.UTC)
            today = now.astimezone(tz).date()
            # The recorder clears asynchronously; after a clear, treat the series as empty.
            last = None if cleared_now else await stats.last_hour(now)
            if last is None:
                start = today - dt.timedelta(days=opts.initial_history_days)
                _LOGGER.info("No existing statistics; importing %d days of history", opts.initial_history_days)
            else:
                last_local = last.astimezone(tz).date()
                start = min(today - dt.timedelta(days=opts.refresh_days), last_local - dt.timedelta(days=1))
            start = max(start, today - dt.timedelta(days=MAX_BACKFILL_DAYS))

            billing: BillingProgress | None = None
            async with PortalClient(opts.portal, opts.username, opts.password) as portal:
                await portal.login()
                records = await portal.hourly(start, today + dt.timedelta(days=1))
                if opts.billing_sensors:
                    try:
                        billing = await portal.billing_progress()
                    except (PortalError, aiohttp.ClientError) as err:
                        _LOGGER.warning("Billing period data unavailable: %s", err)

            valid = [r for r in records if r.gallons is not None]
            _LOGGER.info("Fetched %d hourly records (%s to %s)", len(valid), start, today)

            buckets = bucket(valid, tz)
            if buckets:
                anchor_start, anchor_sum = (None, 0.0) if last is None else await stats.anchor(buckets[0][0])
                rows = ha.fold(buckets, anchor_start, anchor_sum)
                if rows:
                    await stats.import_rows(rows)
                    _LOGGER.info(
                        "Imported %d statistics rows into %s (running total %.0f gal)",
                        len(rows), self.statistic_id, rows[-1]["sum"],
                    )

        await self._publish(valid, tz, billing)

    async def _publish(
        self, records: list[HourRecord], tz: ZoneInfo, billing: BillingProgress | None
    ) -> None:
        values, attrs = sensor_values(records, tz, billing)
        self.arrivals.observe(values.get("data_through"), tz)
        attrs.setdefault("data_through", {}).update(self.arrivals.attributes())
        await self.publisher.publish(values, attrs)
        await self.alerter.success(values.get("data_through"))
        await self.post_status()

    async def update(self) -> dt.timedelta:
        """Run one update and handle its outcome.

        Returns:
            Time to wait before the next update.
        """
        opts = self.opts
        try:
            await self.cycle()
        except AuthError as err:
            _LOGGER.error("Portal login failed: %s (check username and password)", err)
            await self.failed(f"login rejected: {err}", auth=True)
            return opts.auth_retry_interval
        except (PortalError, ha.HAError, aiohttp.ClientError, OSError, TimeoutError) as err:
            _LOGGER.error("Update failed: %s", err)
            await self.failed(_describe(err), auth=False)
            return min(opts.poll_interval, opts.retry_interval)
        except Exception as err:
            _LOGGER.exception("Unexpected error")
            await self.failed(f"unexpected error ({type(err).__name__})", auth=False)
            return min(opts.poll_interval, opts.retry_interval)
        return opts.poll_interval

    async def failed(self, error: str, *, auth: bool) -> None:
        """Record a failed update, alert if due, and update the status sensor."""
        try:
            await self.alerter.failure(error, auth=auth)
            await self.post_status()
        except (ha.HAError, aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("Could not report failure to Home Assistant: %s", err)

    async def post_status(self) -> None:
        await self.publisher.post_status(self.alerter.state.status, self.alerter.status_attributes())

    async def republish(self, reason: str) -> None:
        if await self.publisher.republish():
            _LOGGER.info("Re-posted sensor states (%s)", reason)


async def watch_restarts(poller: Poller, stop: asyncio.Event) -> None:
    """Re-post sensors when Home Assistant restarts.

    REST-created states do not survive an HA restart. The add-on keeps a WebSocket
    subscription to `homeassistant_started`; the connection drops while HA restarts,
    so a successful reconnect also triggers a re-post.
    """
    connected_before = False
    while not stop.is_set():
        try:
            async with ha.HAWebSocket() as ws:
                if connected_before:
                    await poller.republish("Home Assistant reconnected")
                connected_before = True
                async for _event in ws.events("homeassistant_started"):
                    await poller.republish("Home Assistant started")
        except (ha.HAError, aiohttp.ClientError, OSError, TimeoutError) as err:
            _LOGGER.debug("Restart watcher disconnected: %s", err)
        except Exception:
            _LOGGER.exception("Restart watcher error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass


def bucket(records: list[HourRecord], tz: ZoneInfo) -> list[tuple[dt.datetime, float]]:
    """Group local hour records into UTC hour buckets (DST fall-back hours collapse and sum).

    Returns:
        Sorted (utc_start, gallons) pairs.
    """
    out: dict[dt.datetime, float] = defaultdict(float)
    for rec in records:
        utc = rec.start.replace(tzinfo=tz).astimezone(dt.UTC)
        out[utc] += rec.gallons or 0.0
    return sorted(out.items())


def sensor_values(
    records: list[HourRecord], tz: ZoneInfo, billing: BillingProgress | None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Compute sensor states and attributes.

    Returns:
        (values by sensor key, attributes by sensor key)
    """
    values: dict[str, Any] = {}
    attrs: dict[str, dict[str, Any]] = {}

    if records:
        last = records[-1]
        start = last.start.replace(tzinfo=tz)
        values["last_hour_usage"] = last.gallons
        attrs["last_hour_usage"] = {"start": start.isoformat(), "end": (start + dt.timedelta(hours=1)).isoformat()}
        values["data_through"] = (start + dt.timedelta(hours=1)).isoformat()

        by_day: dict[dt.date, list[HourRecord]] = defaultdict(list)
        for rec in records:
            by_day[rec.start.date()].append(rec)
        complete = [d for d, recs in by_day.items() if len(recs) >= 23 and any(r.start.hour == 23 for r in recs)]
        if complete:
            day = max(complete)
            values["last_day_usage"] = round(sum(r.gallons or 0 for r in by_day[day]), 3)
            attrs["last_day_usage"] = {
                "date": day.isoformat(),
                "hourly": [{"hour": r.start.strftime("%H:%M"), "gallons": r.gallons} for r in by_day[day]],
            }

    if billing is not None:
        period = {"period_start": billing.period_start, "period_end": billing.period_end}
        values["billing_period_usage"] = billing.usage_to_date
        attrs["billing_period_usage"] = period
        values["billing_period_projected"] = billing.projected_total
        attrs["billing_period_projected"] = period
        values["billing_period_last_year"] = billing.last_year_total
        attrs["billing_period_last_year"] = {
            "to_same_date": billing.last_year_to_date,
            "prev_start": billing.last_year_start,
            "prev_end": billing.last_year_end,
        }
    return values, attrs


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    try:
        opts = load_options()
    except SystemExit as err:
        _LOGGER.error("%s", err)
        await notify_config_error(str(err))
        raise
    logging.getLogger().setLevel(getattr(logging, opts.log_level.upper(), logging.INFO))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    _LOGGER.info(
        "Portal %s, entity prefix sensor.%s_%s, poll every %s, refresh window %d days",
        opts.portal.base, DOMAIN, opts.portal.site_id, opts.poll_interval, opts.refresh_days,
    )
    if not opts.portal.secure and not opts.portal.local:
        _LOGGER.warning("portal_url uses http; the password is sent unencrypted. Use https.")

    poller = Poller(opts)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await poller.post_status()
    except (ha.HAError, aiohttp.ClientError, OSError) as err:
        _LOGGER.debug("Could not post initial status: %s", err)

    watcher = asyncio.create_task(watch_restarts(poller, stop))

    while not stop.is_set():
        update = asyncio.create_task(poller.update())
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({update, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if not update.done():
            # Stop requested mid-update (add-on stop/restart).
            update.cancel()
            await asyncio.gather(update, return_exceptions=True)
            break
        delay = update.result()
        _LOGGER.debug("Next update in %s", delay)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay.total_seconds())
        except TimeoutError:
            pass

    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    _LOGGER.info("Stopped")


def _describe(err: BaseException) -> str:
    """Short, user-facing error text for alerts (no request bodies or credentials)."""
    if isinstance(err, TimeoutError):
        return "timed out"
    if isinstance(err, aiohttp.ClientConnectorError):
        return f"cannot connect to {err.host}"
    text = str(err) or type(err).__name__
    return text if len(text) <= 200 else text[:197] + "..."


if __name__ == "__main__":
    asyncio.run(run())
