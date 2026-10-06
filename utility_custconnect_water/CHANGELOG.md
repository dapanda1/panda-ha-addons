# Changelog

## 1.0.0

First release.

- Imports hourly water usage from CustConnect utility portals into the Home Assistant long-term statistic `utility_custconnect_water:<site_id>_water_usage` (gallons) for the Energy dashboard.
- `portal_url` accepts a custconnect.com subdomain, a hostname, or a full login page URL, including utilities on their own domain.
- Detects the meter's channel and unit; requests gallons when the portal offers them, otherwise converts from CCF/HCF, cubic feet, cubic meters, liters, thousand gallons, or acre-feet.
- Re-imports a refresh window on every update to pick up the utility's corrections, and fills gaps after downtime (up to 400 days).
- Sensors posted through the Home Assistant REST API: last hour usage, last full day usage, billing period usage / projected / same period last year, data through, and update status. Re-posted automatically after Home Assistant restarts.
- Records when new readings arrive from the portal (`received_at`, `lag_hours`, `recent_arrivals` on the `data_through` sensor) to show the utility's publishing schedule.
- Failure alerts: Home Assistant notification and optional mobile push (`notify_service`), immediate on a rejected login, after a configurable number of failures otherwise, with a "resumed" message on recovery.
- All settings on the Configuration tab with descriptions, ranges, and examples.
- No ports or ingress; outbound connections only. Account and meter numbers are not stored, logged, or published.
