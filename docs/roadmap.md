# Roadmap

The repo started as the WildFireBot name. Milestone 1 is the first implementation.

## Milestone 1 — clients and a smoke-exposure score

Done in this tree.

- FIRMS, Open-Meteo, AirNow, and OpenAQ behind `fetch(bbox, time_range)`.
- Confidence and validity filters, FRP-weighted clusters, back-trajectory score.
- EPA 2024 PM2.5 breakpoint conversion.
- CLI `aqi-smoke forecast` (and `wildfirebot forecast`) on live feeds by default.
- Retries, cache with staleness warnings, and a DEGRADED MODE section when a feed is down.
- Tool handlers in `wildfirebot.tools` so the next server does not reimplement the forecast.

## Milestone 2 — PM2.5 and AQI model

`predict_pm25` is a stub on purpose.

- Build an hourly table of smoke score, wind speed, boundary-layer height, distance to the nearest cluster, total upwind FRP, and the previous monitor PM2.5.
- Labels come from AirNow and OpenAQ. Train a regression for PM2.5 and then map it through the breakpoint function.
- Split by time, not at random, so a fire's later hours are not in the training set.
- Report MAE, RMSE, and how often the AQI category matches the monitor. Compare with persistence (last hour's PM2.5) and with a score-only regression.
- Keep the score available when the model cannot run.

## Milestone 3 — MCP server, then a map

The MCP server is done. `wildfirebot-mcp` speaks stdio by default and streamable HTTP with `--http`. It exposes the same handlers as the CLI:

- `forecast_smoke` for a lat/lon and an hour window, including degraded-mode notices.
- `forecast_smoke_aqi` for the Gaussian plume PM2.5 and EPA AQI horizons.
- `pm25_to_aqi` for the breakpoint conversion.
- A later `predict_pm25` tool once milestone 2 has a model worth calling.

Keys stay in `FIRMS_MAP_KEY`, `AIRNOW_API_KEY`, and `OPENAQ_API_KEY`. A missing key is a notice, not a crash.

Still open: a map and an hourly chart as another client of these tools (clusters, the back-trajectory, the score, and the monitor points). That view should call the tools, not a second forecast.
