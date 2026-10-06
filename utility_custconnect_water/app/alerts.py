"""Failure alerts: Home Assistant persistent notification (browser) and optional mobile push.

Alert state is saved in /data so an add-on restart during an outage neither repeats the alert
nor loses the recovery message.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import json
import logging
import os
import re
from typing import Any

import ha

_LOGGER = logging.getLogger("custconnect.alerts")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TITLE = "Utility CustConnect Water"
_SERVICE_RE = re.compile(r"^[a-z0-9_]+$")

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_AUTH = "auth_failed"
STATUS_STARTING = "starting"


def normalize_service(value: str | None) -> str | None:
    """Accept 'mobile_app_x' or 'notify.mobile_app_x'.

    Returns:
        The service name without the domain, or None when empty.

    Raises:
        ValueError: Not a valid service name.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    text = text.removeprefix("notify.")
    if not _SERVICE_RE.match(text):
        raise ValueError(f"'{value}' is not a notify service name (example: mobile_app_pixel_10_pro)")
    return text


@dataclass
class AlertState:
    status: str = STATUS_STARTING
    consecutive_failures: int = 0
    first_failure: str | None = None
    last_error: str | None = None
    last_error_time: str | None = None
    last_success: str | None = None
    alerted: bool = False


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool
    after_failures: int
    on_recovery: bool
    notify_service: str | None
    high_priority: bool
    retry_minutes: int
    auth_retry_minutes: int


class Alerter:
    """Tracks consecutive failures and sends alerts on threshold, login failure, and recovery."""

    def __init__(self, config: AlertConfig, site_id: str, portal_host: str) -> None:
        self.config = config
        self.portal_host = portal_host
        self.notification_id = f"utility_custconnect_water_{site_id}"
        self._path = os.path.join(DATA_DIR, f"alert_state_{site_id}.json")
        self.state = self._load()
        # Set by the poller from the portal/Home Assistant time zone; used for message text.
        self.tz: dt.tzinfo | None = None

    # ---------------------------------------------------------------- state

    def _load(self) -> AlertState:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            return AlertState(**{k: v for k, v in data.items() if k in AlertState.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return AlertState()

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(asdict(self.state), fh)
        except OSError as err:
            _LOGGER.warning("Could not save alert state: %s", err)

    # --------------------------------------------------------------- events

    async def success(self, data_through: str | None) -> None:
        was_alerted = self.state.alerted
        self.state = AlertState(status=STATUS_OK, last_success=_now())
        self._save()
        if was_alerted:
            await self._dismiss()
            if self.config.on_recovery:
                when = f" Data is available through {self._pretty(data_through)}." if data_through else ""
                await self._send(
                    "Water usage updates resumed",
                    f"Updates from {self.portal_host} are working again.{when}",
                    persistent=False,
                )

    async def failure(self, error: str, *, auth: bool) -> None:
        s = self.state
        s.consecutive_failures += 1
        s.first_failure = s.first_failure or _now()
        s.last_error = error
        s.last_error_time = _now()
        s.status = STATUS_AUTH if auth else STATUS_ERROR
        self._save()

        if not self.config.enabled:
            return
        # A rejected login does not fix itself; alert on the first one.
        due = auth or s.consecutive_failures >= self.config.after_failures
        if not due:
            return

        if auth:
            title = "Water portal login failed"
            message = (
                f"The portal at {self.portal_host} rejected the username or password. "
                f"Updates are paused and retried every {self.config.auth_retry_minutes} minutes. "
                "Check the username and password on the add-on Configuration tab."
            )
        else:
            title = "Water usage updates failing"
            message = (
                f"{s.consecutive_failures} updates in a row from {self.portal_host} have failed "
                f"since {self._pretty(s.first_failure)}. Last error: {error}. "
                f"Retrying every {self.config.retry_minutes} minutes; see the add-on log for details."
            )
        # The browser notification is replaced on every failure so it shows the latest count;
        # the mobile push is sent once per outage.
        await self._send(title, message, persistent=True, push=not s.alerted)
        if not s.alerted:
            s.alerted = True
            self._save()

    # -------------------------------------------------------------- sending

    async def _send(self, title: str, message: str, *, persistent: bool, push: bool = True) -> None:
        if persistent:
            try:
                await ha.call_service(
                    "persistent_notification",
                    "create",
                    {"title": f"{TITLE}: {title}", "message": message, "notification_id": self.notification_id},
                )
            except ha.HAError as err:
                _LOGGER.warning("Could not create notification: %s", err)
        if push and self.config.notify_service:
            data: dict[str, Any] = {"tag": self.notification_id}
            if self.config.high_priority:
                # Android companion app: deliver immediately; iOS: time-sensitive.
                data.update({"ttl": 0, "priority": "high", "push": {"interruption-level": "time-sensitive"}})
            try:
                await ha.call_service(
                    "notify",
                    self.config.notify_service,
                    {"title": f"{TITLE}: {title}", "message": message, "data": data},
                )
            except ha.HAError as err:
                _LOGGER.warning("Could not send to notify.%s: %s", self.config.notify_service, err)

    async def _dismiss(self) -> None:
        try:
            await ha.call_service(
                "persistent_notification", "dismiss", {"notification_id": self.notification_id}
            )
        except ha.HAError as err:
            _LOGGER.debug("Could not dismiss notification: %s", err)

    def _pretty(self, iso: str | None) -> str:
        if not iso:
            return "unknown"
        try:
            value = dt.datetime.fromisoformat(iso)
        except ValueError:
            return iso
        if self.tz and value.tzinfo:
            value = value.astimezone(self.tz)
        return value.strftime("%Y-%m-%d %H:%M")

    # --------------------------------------------------------------- sensor

    def status_attributes(self) -> dict[str, Any]:
        s = self.state
        return {
            "last_success": s.last_success,
            "last_error": s.last_error,
            "last_error_time": s.last_error_time,
            "consecutive_failures": s.consecutive_failures,
            "icon": "mdi:check-circle" if s.status == STATUS_OK else "mdi:alert-circle",
        }


async def notify_config_error(message: str) -> None:
    """Best-effort browser notification when the add-on cannot start because of its options."""
    try:
        await ha.call_service(
            "persistent_notification",
            "create",
            {
                "title": f"{TITLE}: add-on stopped",
                "message": f"Invalid configuration: {message}. Fix it on the add-on Configuration tab and start the add-on.",
                "notification_id": "utility_custconnect_water_config",
            },
        )
    except (ha.HAError, OSError) as err:
        _LOGGER.debug("Could not create configuration notification: %s", err)


def _now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
