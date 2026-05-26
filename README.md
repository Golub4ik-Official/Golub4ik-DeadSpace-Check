# DeadSpace14 Ban Evasion Detector

DeadSpace14 Ban Evasion Detector is a Discord automation tool that links Discord telemetry with DeadSpace14 admin panel data to surface probable ban evasions in near real time. The project targets experienced server administrators who need reproducible evidence trails and automated triage of suspicious accounts.

## Capabilities

- Multi-layer correlation across HWIDs, IPs, timestamps, and prior ban records with configurable confidence thresholds.
- Automated Discord ingestion that parses "Arrived new player" events and complaint channels to build account graphs.
- Structured output via JSON (`reports/scan_report.json`) and console logs for integration with downstream tooling.
- Caching, concurrency control, and throttling tuned for high-volume community servers.

## Quick Start

```bash
git clone https://github.com/yourusername/deadspace14-ban-detector.git
cd deadspace14-ban-detector
python -m venv .venv
.venv\Scripts\activate  # PowerShell
pip install -r requirements.txt
copy config.py config_local.py  # optional override
python main.py --check-ban-bypass
```

## Configuration

Runtime settings live in `config.py`. Override sensitive values before first execution.

| Key | Purpose |
| --- | --- |
| `DISCORD_USER_TOKEN` | Discord token used for message scraping |
| `TARGET_CHANNEL_ID` | Channel ID emitting new-player notices |
| `COMPLAINT_CHANNEL_IDS` | Channels searched for nick-based complaints |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | DeadSpace14 admin credentials |
| `MAX_CONCURRENT_REQUESTS` | Upper bound for parallel HTTP calls |
| `CHECK_BAN_BYPASS` / `BAN_BYPASS_PAGES` | Ban-hit scraping behavior |

Tune detection sensitivity with the `*_THRESHOLD_MINUTES` and `MESSAGE_LIMIT` values. Command-line overrides are available through `main.py --help`.

## Execution Patterns

- Baseline monitoring: `python main.py`
- Focused ban-bypass sweep: `python main.py --check-ban-bypass --ban-bypass-pages 10`
- Single user investigation: `python main.py --username <nick>`

All modes emit artifacts in `reports/` and populate `complaint_message_cache.json` for incremental scanning.

## Architecture Overview

```
admin_panel.py            DeadSpace14 panel scraping
bot.py                    Discord coordination layer
core/scanner.py           Message ingestion and job queueing
core/analyzer.py          Correlation engine and scoring
services/*.py             API clients, caching, reporting
models/*.py               Typed payload representations
utils/*.py                Async, logging, and formatting helpers
```

The codebase is asyncio-first and leans on request batching with per-service rate guards. `services/reporting/formatter.py` shapes the JSON payloads consumed by downstream dashboards.

## Operational Notes

- Respect Discord and DeadSpace14 rate limits; reduce `MAX_CONCURRENT_REQUESTS` or increase `REQUEST_TIMEOUT` when throttled.
- Persist configuration secrets outside of source control (`config_local.py`, environment variables, or secret managers).
- Validate Discord permissions before first run; the detector assumes read access to configured channels.

## License and Intent

Distributed under the MIT License. Use is restricted to legitimate moderation and security workflows. The maintainers do not support harassment, privacy violations, or any activity that violates platform terms of service.