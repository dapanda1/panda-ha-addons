# Utility CustConnect Water

Imports hourly water usage from utility portals built on CustConnect (the "HomeConnect" customer portal, normally at `<utility>.custconnect.com/HomeConnect`, for example Santa Rosa Water) into Home Assistant. Utilities that serve the same portal from their own domain are supported through a full URL.

> Unofficial add-on, not affiliated with CustConnect or any utility. It signs in to your portal with your credentials and reads the usage data the portal shows you; you are responsible for complying with your utility's portal terms of use.

All settings are on the add-on **Configuration** tab. Optional settings are listed under **Show unused optional configuration options**. Restart the add-on after saving changes.

## Output

### Long-term statistic

`utility_custconnect_water:<site_id>_water_usage` (gallons, hourly, cumulative sum).

Add it under **Settings → Dashboards → Energy → Water consumption**. Each hour's usage is stored in the hour it occurred.

### Sensors

Sensors are posted through the Home Assistant REST API (`/api/states`), the same method used by the other add-ons in this repository.

| Entity | Description |
|---|---|
| `sensor.utility_custconnect_water_<site_id>_last_hour_usage` | Gallons in the most recent hour available |
| `sensor.utility_custconnect_water_<site_id>_last_day_usage` | Gallons on the most recent complete day; `hourly` attribute lists each hour |
| `sensor.utility_custconnect_water_<site_id>_billing_period_usage` | Usage to date in the current billing period |
| `sensor.utility_custconnect_water_<site_id>_billing_period_projected` | Portal's projected total for the current billing period |
| `sensor.utility_custconnect_water_<site_id>_billing_period_last_year` | Total for the same billing period last year |
| `sensor.utility_custconnect_water_<site_id>_data_through` | End time of the most recent hourly reading; attributes `received_at` (when the add-on first saw it), `lag_hours`, and `recent_arrivals` (last 14 batches) |
| `sensor.utility_custconnect_water_<site_id>_status` | `ok`, `error`, `auth_failed`, or `starting`; attributes `last_success`, `last_error`, `last_error_time`, `consecutive_failures` |

The three billing sensors are posted only when **Billing period sensors** is on.

REST-created entities have no unique ID, so they cannot be renamed or assigned an area in the UI; use them by entity ID in dashboards, templates and automations. The add-on watches for Home Assistant restarts and re-posts the last values as soon as Home Assistant is back, without logging in to the portal again.

Use the statistic, not the sensors, for the Energy dashboard.

A sample card is in `dashboard_card.yaml`.

## Configuration

### Portal

| Option | Default | Description |
|---|---|---|
| `portal_url` | | Login page URL or custconnect.com subdomain (see below) |
| `username` | | Portal username |
| `password` | | Portal password |
| `time_zone` | *(HA time zone)* | Utility time zone, e.g. `America/Los_Angeles` |

#### portal_url

Open your utility's portal in a browser, go to the login page, and copy the address.

| Entered value | Portal used | Site ID |
|---|---|---|
| `santarosa` | `https://santarosa.custconnect.com/HomeConnect` | `santarosa` |
| `santarosa.custconnect.com` | `https://santarosa.custconnect.com/HomeConnect` | `santarosa` |
| `https://santarosa.custconnect.com/HomeConnect/login.html` | `https://santarosa.custconnect.com/HomeConnect` | `santarosa` |
| `https://water.example.gov/HomeConnect/login.html` | `https://water.example.gov/HomeConnect` | `water_example_gov` |
| `https://water.example.gov/portal/login.html` | `https://water.example.gov/portal` | `water_example_gov` |

- The site ID is the custconnect.com subdomain, or the full hostname for other domains, with characters other than letters and digits replaced by `_`. It is used in entity and statistic IDs and is shown in the add-on log at start.
- `https` is assumed when no scheme is given. `http` works but sends the password unencrypted; the log shows a warning.
- A username or password inside the URL is rejected; use the `username` and `password` options.
- The portal must be the same CustConnect "HomeConnect" application (login page with `Controller/Login`). Other utility portal products are not supported.

### Schedule

| Option | Default | Range | Description |
|---|---|---|---|
| `poll_interval_minutes` | `60` | 15–1440 | Time between updates (one portal login each) |
| `retry_minutes` | `15` | 5–240 | Retry after network, portal, or HA errors |
| `auth_retry_minutes` | `60` | 15–1440 | Retry after a rejected login |

Hourly readings appear on the portal in a batch, not in real time. In testing with Santa Rosa, a full day of hourly readings arrived together the following day: at 12:11 PM on Oct 5 and again at 5:12 AM on Oct 6, the newest reading ended at midnight Oct 4–5, about 29 hours earlier. Expect readings to be 12–36 hours old. Polling more often than the portal updates gains nothing; a longer interval loses no data, because every update re-reads the refresh window.

### Statistics

| Option | Default | Description |
|---|---|---|
| `initial_history_days` | `30` | History imported when the statistic is empty (1–400) |
| `refresh_days` | `7` | Days re-fetched each update to pick up corrections (2–60) |
| `clear_statistics_on_start` | `false` | Delete the statistic and re-import on the next start; turn off afterwards |
| `statistic_name` | *(Water usage (portal hostname))* | Display name of the statistic |

### Sensors

| Option | Default | Description |
|---|---|---|
| `billing_sensors` | `true` | Post the billing period sensors |

### Alerts

| Option | Default | Description |
|---|---|---|
| `notify_on_error` | `true` | Send alerts when updates fail |
| `notify_after_failures` | `3` | Consecutive failures before alerting (1–24); login failures alert at once |
| `notify_on_recovery` | `true` | Send a "resumed" mobile message when updates work again |
| `notify_service` | | Mobile device notify service, e.g. `mobile_app_pixel_10_pro` |
| `notify_high_priority` | `true` | High priority (Android) / time-sensitive (iOS) mobile alerts |

How alerts are delivered:

| Event | Home Assistant notification (browser and app) | Mobile push (`notify_service`) |
|---|---|---|
| Login rejected | Created immediately | Sent immediately |
| Other failures, below the threshold | — | — |
| Other failures, at the threshold | Created | Sent once per outage |
| Further failures | Updated with the latest count and error | Not repeated |
| Updates resume | Removed | "Resumed" message (if `notify_on_recovery`) |
| Invalid options at start | Created; the add-on stops | — |

- Mobile alerts use the tag `utility_custconnect_water_<site_id>`, so the "resumed" message replaces the failure alert on the phone.
- Alert state is kept in the add-on's `/data` folder, so restarting the add-on during an outage does not send a duplicate alert.
- Alerts contain the portal hostname and the error type only; no username, password, or account data.
- The `status` sensor can be used in your own automations, e.g. trigger when it is not `ok` for 6 hours.

### Logging

| Option | Default | Description |
|---|---|---|
| `log_level` | `info` | `debug`, `info`, `warning`, `error` |

## Units

The add-on reads the meter's channel and units from the portal at each sign-in:

- If the portal offers gallons, gallons are requested directly.
- Otherwise the reported unit is converted to US gallons: CCF/HCF (748.052), cubic feet (7.48052), thousand cubic feet (7,480.52), cubic meters (264.172), liters (0.264172), thousand gallons (1,000), acre-feet (325,851).
- A meter that only reports a non-volume unit stops the update with an error naming the units found.

The statistic is stored in gallons; Home Assistant displays it in your configured unit system (e.g. liters or m³ for metric).

## When the portal publishes

Every time an update sees newer readings, the add-on records when they first appeared (add-on log line `New readings: ...`, and the `received_at`, `lag_hours`, and `recent_arrivals` attributes of the `data_through` sensor; kept across restarts). After a few days `recent_arrivals` shows your utility's publishing schedule. The accuracy is the poll interval: to pin it down, set `poll_interval_minutes` to 15 for a few days, then raise it to just cover the arrival time.

## Limitations

- One water meter per install: the portal's default water meter is used. Accounts with several meters or linked accounts monitor the default one only.
- Verified with Santa Rosa Water. Other utilities work if they run the same CustConnect "HomeConnect" portal application.
- Readings arrive in a daily batch and are 12–36 hours old (observed with Santa Rosa). "Last hour usage" is the newest hour the portal has published, not the current hour. `data_through` shows the newest reading available.

## Behavior

- Each update logs in, fetches hourly usage, and re-imports the refresh window. Re-imported hours overwrite existing rows; the running total continues from the last row before the window.
- After downtime longer than the refresh window, the next update starts from the last recorded hour (up to 400 days back).
- `data_through` shows how current the portal data is.
- Changing `portal_url` to a different site creates a new statistic and new sensors; the old statistic remains until removed.
- Removed or renamed options are cleaned up automatically on start (`migrate.py`).

## Network and permissions

- No ports, no ingress: the add-on only makes outbound connections (to the portal URL, and the Supervisor proxy for the Home Assistant API).
- `homeassistant_api: true` is used for statistics import and sensor states. No `hassio_api`, host network, or folder mappings.

## Removing data

Uninstalling the add-on leaves the statistic in the recorder. Remove it under **Developer tools → Statistics**.
