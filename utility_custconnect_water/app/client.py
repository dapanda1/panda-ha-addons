"""CustConnect utility portal client.

The portal application ("HomeConnect") is normally served at
https://<utility>.custconnect.com/HomeConnect; utilities can also use their own domain.
Endpoints under <portal base>/Controller:
  POST Login                         form: username, password, ajax=true -> {"redirectUrl": ...}
  GET  InitializationStatus          poll until initializationComplete
  GET  GetAccountDetails             -> currentAccountId
  GET  GetMyProgressDefaultData      ?forceInitialCommodityTp=W -> meterId, channel, outputUnit
  GET  MeterDetails                  channel names and output units for the meter
  POST Usage                         JSON, rollup "Hour" -> results[0] = [["YYYY-MM-DD HH:MM", gal, null], ...]
  GET  GetMyProgress                 billing-period totals

Hourly rows are labeled by the END of the hour in the utility's local time;
records returned here are labeled by hour START (naive local datetimes).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp

_LOGGER = logging.getLogger("custconnect.client")

_DATE_FMT = "%Y-%m-%d %H:%M:%S"
_ROW_FMTS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
_TIMEOUT = aiohttp.ClientTimeout(total=60)


class PortalError(Exception):
    """Unexpected portal response."""


class AuthError(PortalError):
    """Login rejected."""


@dataclass
class HourRecord:
    """One hour of usage; ``start`` is the local wall-clock hour start (naive)."""

    start: dt.datetime
    gallons: float | None


@dataclass
class BillingProgress:
    """Current billing period totals."""

    period_start: str | None = None
    period_end: str | None = None
    usage_to_date: float | None = None
    projected_total: float | None = None
    last_year_total: float | None = None
    last_year_to_date: float | None = None
    last_year_start: str | None = None
    last_year_end: str | None = None


# Volume units the portal may report, to US gallons. Keys are upper-case with
# non-alphanumerics removed (e.g. "Cu Ft" -> "CUFT").
GALLONS_PER_UNIT: dict[str, float] = {
    "GAL": 1.0, "GALLON": 1.0, "GALLONS": 1.0,
    "KGAL": 1000.0, "TGAL": 1000.0, "1000GAL": 1000.0,
    "CCF": 748.052, "HCF": 748.052, "100CF": 748.052,
    "CF": 7.48052, "FT3": 7.48052, "CUFT": 7.48052, "CUBICFEET": 7.48052,
    "KCF": 7480.52, "MCF": 7480.52,
    "M3": 264.172, "CM": 264.172, "CUBICMETERS": 264.172,
    "L": 0.264172, "LITER": 0.264172, "LITERS": 0.264172, "LITRE": 0.264172,
    "AF": 325851.0, "ACFT": 325851.0,
}


def gallons_per(unit: str | None) -> float | None:
    """Conversion factor from a portal unit to US gallons.

    Returns:
        Gallons per unit, or None when the unit is not a known volume unit.
    """
    key = re.sub(r"[^A-Z0-9]", "", (unit or "").upper())
    return GALLONS_PER_UNIT.get(key)


DEFAULT_DOMAIN = "custconnect.com"
DEFAULT_APP_PATH = "/HomeConnect"
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


@dataclass(frozen=True)
class Portal:
    """Resolved portal location."""

    origin: str  # scheme://host[:port]
    base: str  # origin + application path, e.g. https://santarosa.custconnect.com/HomeConnect
    hostname: str  # e.g. santarosa.custconnect.com
    site_id: str  # slug used in entity and statistic IDs, e.g. santarosa

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def local(self) -> bool:
        if self.hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.hostname).is_loopback
        except ValueError:
            return False


def parse_portal(value: str) -> Portal:
    """Resolve the portal_url option.

    Accepted forms:
      santarosa                                              -> https://santarosa.custconnect.com/HomeConnect
      santarosa.custconnect.com                              -> https://santarosa.custconnect.com/HomeConnect
      https://santarosa.custconnect.com/HomeConnect/login.html
      https://water.example.gov/HomeConnect/login.html       -> utility on its own domain
      https://water.example.gov/portal/login.html            -> application path /portal

    Returns:
        The resolved portal.

    Raises:
        ValueError: The value cannot be used as a portal address.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("portal_url is empty")
    if "://" not in text:
        if "." not in text and "/" not in text and ":" not in text:
            text = f"{text}.{DEFAULT_DOMAIN}"
        text = f"https://{text}"

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError(f"unsupported scheme '{parts.scheme}' (use https)")
    if parts.username or parts.password:
        raise ValueError("credentials must not be part of the URL; use the username and password options")
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise ValueError("no hostname in portal_url")
    is_ip = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip = False
    if not is_ip and not _HOST_RE.match(hostname):
        raise ValueError(f"invalid hostname '{hostname}'")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError("invalid port in portal_url") from err

    netloc = f"[{hostname}]" if is_ip and ":" in hostname else hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    origin = f"{scheme}://{netloc}"

    # Application path: everything up to and including a "HomeConnect" segment; otherwise the
    # given directory, dropping a trailing page such as login.html; otherwise the default.
    segments = [s for s in parts.path.split("/") if s]
    app_index = next((i for i, s in enumerate(segments) if s.lower() == "homeconnect"), None)
    if app_index is not None:
        segments = segments[: app_index + 1]
    else:
        if segments and "." in segments[-1]:
            segments = segments[:-1]
        if not segments:
            segments = [DEFAULT_APP_PATH.strip("/")]
    base = origin + "/" + "/".join(segments)

    suffix = f".{DEFAULT_DOMAIN}"
    site = hostname[: -len(suffix)] if hostname.endswith(suffix) else hostname
    site_id = re.sub(r"[^a-z0-9]+", "_", site).strip("_") or "portal"
    return Portal(origin=origin, base=base, hostname=hostname, site_id=site_id)


class PortalClient:
    """Session-scoped client; create one per poll cycle."""

    def __init__(self, portal: Portal, username: str, password: str) -> None:
        self.portal = portal
        self._username = username
        self._password = password
        self._origin = portal.origin
        self._base = portal.base
        self._session: aiohttp.ClientSession | None = None
        self.meter_id: str | None = None
        self.channel = "GAL"
        self.unit = "GAL"
        self.factor = 1.0

    async def __aenter__(self) -> PortalClient:
        self._session = aiohttp.ClientSession(
            timeout=_TIMEOUT, cookie_jar=aiohttp.CookieJar(unsafe=True)
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()

    async def login(self) -> None:
        """Log in and resolve account and water meter.

        Only a JSON login response without a redirect counts as rejected credentials. HTTP errors
        and non-JSON pages (outage, maintenance page, wrong portal_url) are portal errors, so they
        use the normal retry interval and do not trigger a login-failure alert.

        Raises:
            AuthError: Credentials rejected.
            PortalError: Portal unavailable, unexpected response, or account/meter missing.
        """
        assert self._session
        headers = {**_HEADERS, "Origin": self._origin, "Referer": f"{self._base}/login.html"}
        async with self._session.post(
            f"{self._base}/Controller/Login",
            data={"username": self._username, "password": self._password, "ajax": "true"},
            headers=headers,
        ) as resp:
            status = resp.status
            text = await resp.text()
        if status >= 400:
            raise PortalError(f"login: HTTP {status}")
        try:
            data = json.loads(text)
        except ValueError as err:
            raise PortalError(
                f"login: unexpected non-JSON response (HTTP {status}); portal down or portal_url wrong"
            ) from err
        if not isinstance(data, dict) or not data.get("redirectUrl"):
            detail = "; ".join(
                str(v) for k, v in (data.items() if isinstance(data, dict) else [])
                if "error" in k.lower() or "message" in k.lower()
            )
            raise AuthError(detail or "login rejected")

        for _ in range(60):
            status = await self._get("InitializationStatus")
            if not isinstance(status, dict) or status.get("initializationComplete"):
                break
            await asyncio.sleep(1)
        else:
            _LOGGER.warning("Portal initialization did not complete; continuing")

        details = await self._get("GetAccountDetails")
        if not (details.get("currentAccountId") or details.get("defaultAccountId")):
            raise PortalError("no account in GetAccountDetails")

        defaults = await self._get("GetMyProgressDefaultData", forceInitialCommodityTp="W")
        self.meter_id = str(defaults.get("meterId") or "")
        if not self.meter_id:
            raise PortalError("no water meter in GetMyProgressDefaultData")
        await self._resolve_units(defaults.get("channel"), defaults.get("outputUnit"))
        _LOGGER.debug("Logged in; channel %s, unit %s (x%s to gallons)", self.channel, self.unit, self.factor)

    async def _resolve_units(self, default_channel: str | None, default_unit: str | None) -> None:
        """Pick the meter channel and output unit; prefer gallons, otherwise convert.

        Raises:
            PortalError: The meter only reports units that are not volumes.
        """
        channels = await self._meter_details(getChannelNames="true")
        channel = default_channel if default_channel in channels or not channels else None
        candidates = [channel] if channel else []
        candidates += [c for c in channels if c not in candidates]
        if not candidates:
            candidates = [default_channel or "GAL"]

        seen: list[str] = []
        for name in candidates:
            units = await self._meter_details(getOutputUOMs="true", channelName=name)
            if not units:
                units = [default_unit or name]
            seen += [f"{name}:{u}" for u in units]
            gallon_unit = next((u for u in units if gallons_per(u) == 1.0), None)
            chosen = gallon_unit or next((u for u in units if gallons_per(u)), None)
            if chosen:
                self.channel, self.unit, self.factor = name, chosen, gallons_per(chosen) or 1.0
                if self.factor != 1.0:
                    _LOGGER.info("Meter reports %s; converting to gallons (x%s)", chosen, self.factor)
                return
        raise PortalError(f"meter has no supported volume unit (channel:unit = {', '.join(seen)})")

    async def _meter_details(self, **params: str) -> list[str]:
        try:
            data = await self._get(
                "MeterDetails", meterId=self.meter_id, hideIntervalChannels="false",
                hideRegisterChannels="false", **params,
            )
        except PortalError as err:
            _LOGGER.debug("MeterDetails %s unavailable: %s", params, err)
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    async def hourly(self, start: dt.date, end: dt.date) -> list[HourRecord]:
        """Fetch hourly usage covering local days [start, end).

        Returns:
            Records labeled by hour start, oldest first.
        """
        rows = await self._usage(start, end, "Hour")
        parsed = [(_parse_ts(r[0]), self._gal(r[1] if len(r) > 1 else None)) for r in rows]
        if len(parsed) >= 2 and parsed[1][0] - parsed[0][0] == dt.timedelta(hours=1):
            return [HourRecord(ts - dt.timedelta(hours=1), gal) for ts, gal in parsed]
        raise PortalError(f"hourly data not returned ({len(parsed)} rows)")

    async def billing_progress(self) -> BillingProgress:
        """Fetch current billing period usage.

        Returns:
            Parsed totals; fields are None when absent.
        """
        data = await self._get(
            "GetMyProgress", meterId=self.meter_id, channel=self.channel, outputUnit=self.unit, index=""
        )
        result = BillingProgress(
            period_start=data.get("currStartDate"),
            period_end=data.get("currEndDate"),
            last_year_start=data.get("prevStartDate"),
            last_year_end=data.get("prevEndDate"),
        )
        for group in data.get("chartDataList") or []:
            for member in group.get("members") or []:
                dataset = (member.get("detail") or {}).get("datasetId")
                subs = {
                    (s.get("detail") or {}).get("datasetId"): self._gal(s.get("value"))
                    for s in member.get("subTotals") or []
                }
                if dataset == "currUsage":
                    result.projected_total = self._gal(member.get("total"))
                    result.usage_to_date = subs.get("currUsageStart")
                elif dataset == "prevUsage":
                    result.last_year_total = self._gal(member.get("total"))
                    result.last_year_to_date = subs.get("prevUsageStart")
        return result

    async def _usage(self, start: dt.date, end: dt.date, rollup: str) -> list[list[Any]]:
        payload = {
            "inputParameters": [{
                "type": "Enumeration", "paramId": "DatasetType", "isRequired": True,
                "enumValues": ["None", "Weather", "Average"], "defaultValue": "None", "value": "None",
            }],
            "showCostBar": False,
            "meterId": self.meter_id,
            "exportFullIntervalData": False,
            "startDtm": dt.datetime.combine(start, dt.time()).strftime(_DATE_FMT),
            "endDtm": dt.datetime.combine(end, dt.time()).strftime(_DATE_FMT),
            "showHourlyUsageMaxDays": 0,
            "displayMidnightOverlayRegister": False,
            "trimToMidnight": False,
            "showDetailsInLegend": False,
            "channel": self.channel,
            "uom": self.unit,
            "showHourlyUsageStatus": False,
            "rollup": rollup,
        }
        data = await self._request("POST", "Usage", json=payload)
        results = data.get("results") or []
        return results[0] if results and isinstance(results[0], list) else []

    def _gal(self, value: Any) -> float | None:
        number = _num(value)
        return None if number is None else _round(number * self.factor)

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._session
        headers = {**_HEADERS, "Origin": self._origin, "Referer": f"{self._base}/main.jsp"}
        async with self._session.request(
            method, f"{self._base}/Controller/{path}", headers=headers, **kwargs
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise PortalError(f"{method} {path}: HTTP {resp.status}")
        try:
            return json.loads(text)
        except ValueError as err:
            raise PortalError(f"{method} {path}: non-JSON response (session expired?)") from err


def _round(value: float) -> float:
    return round(value, 3)


def _parse_ts(value: str) -> dt.datetime:
    for fmt in _ROW_FMTS:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise PortalError(f"unrecognized timestamp {value!r}")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
