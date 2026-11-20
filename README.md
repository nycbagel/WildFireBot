# WildFireBot

Satellite fire detections and the forecast wind, turned into an hourly smoke-exposure score for one location.

## Inspiration

People downwind of a wildfire often find out from the sky, not from a monitor on their block. NASA FIRMS already sees the hotspots, including ultra-real-time detections over the US and Canada, and forecast models already know which way the wind is about to blow. This project is for the Berkeley x Stanford Grok Bot Hackathon, Most Technically Complex track: make that space data useful to someone standing in the plume.

The repository was renamed to WildFireBot and had no code yet. This tree is the first implementation.

## What it does

`aqi-smoke forecast --lat 37.8 --lon -122.3 --hours 48` prints, for that point:

- fire clusters inside the search radius, with total FRP, distance, and whether a detection is ultra-real-time
- a wind summary at 10 m and about 850 hPa, plus boundary-layer height
- an hourly smoke-exposure score that is high when FRP is upwind, close, and trapped in a shallow boundary layer
- the nearest PM2.5 readings, converted with the EPA 2024 breakpoints when a concentration is present

The score is a relative index. A value near 1 is roughly a 100 MW cluster sitting on the upwind path under an 800 m boundary layer. It is not micrograms per cubic meter, not a regulatory AQI, and not a health advisory. Use AirNow, local smoke outlooks, and public-health guidance for decisions about going outside.

`aqi-smoke aqi --lat 37.8 --lon -122.3` gives an estimated PM2.5 and EPA AQI at +0, 6, 12, 24, and 48 hours. It adds a Gaussian smoke plume from each fire cluster (emission from FRP, carried by the forecast wind) to Open-Meteo's background PM2.5 forecast. It needs no history, so it works for a fire that started an hour ago. It is a screening estimate, not a regulatory AQI. Add `--json` for the same output the `forecast_smoke_aqi` tool returns.

If a feed is down, the command still prints what it has and labels the gap. Missing API keys, a failed satellite, a stale cache, and hours with no wind show up under `DEGRADED MODE` instead of a traceback or a fake zero.

## How we built it (tech stack)

Python 3.11+ package `wildfirebot`, installable with `pyproject.toml`. Console scripts are `aqi-smoke` and `wildfirebot`. Tables are pandas. HTTP is the standard library, with retries, a disk cache, and redacted keys. Lint is ruff. Tests are pytest. GitHub Actions runs both on pull requests.

The package is split the way later milestones need it:

- `sources/` — FIRMS, Open-Meteo (wind and background PM2.5), AirNow, OpenAQ. Each one implements `fetch(bbox, time_range) -> SourceResult`.
- `fires/` — drop low-confidence and invalid hotspots, then cluster the rest by distance and FRP.
- `transport/` — fill short wind gaps, walk a back-trajectory, compute the score.
- `model/` — the EPA PM2.5-to-AQI conversion and the Gaussian plume (`model/plume.py`). The trained model is still a stub.
- `aqi_forecast.py` — background PM2.5 plus plume smoke per horizon, converted to AQI.
- `tools.py` and `mcp.py` — JSON tool handlers (`forecast_smoke`, `forecast_smoke_aqi`, `pm25_to_aqi`). The CLI uses the same forecast function. Milestone 3 is an MCP server over these handlers, not a second copy of the science.

Data sources, and the terms they ask users to keep:

- NASA FIRMS (LANCE / EOSDIS): active-fire detections. https://firms.modaps.eosdis.nasa.gov/
- AirNow: monitor observations. https://www.airnow.gov/
- OpenAQ: open air-quality measurements. https://openaq.org/
- Open-Meteo: forecast wind and boundary-layer height. https://open-meteo.com/

## Challenges we ran into

FIRMS does not have a separate ultra-real-time URL. URT and RT rows ride inside the NRT products and disappear once the NRT detection is processed, so the client has to read the `version` field.

Adding FRP from NOAA-21, NOAA-20, S-NPP, and MODIS for the same flame double-counts the fire. Lower-priority satellites only contribute hotspots that are not already next to a kept detection.

A back-trajectory that is scored by "distance from the fire to the nearest point, including the person" makes every nearby fire look upwind. The score only counts a fire when the upwind path actually gets closer to it. Fires within 5 km still count, because the people are already there.

Wind direction cannot be averaged in degrees. 350° and 10° would become south. Gaps are filled in east/north components.

The four feeds fail independently, and some need keys. The forecast has to keep a partial answer, say which source died, and refuse to print a confident score when the fires or the arrival wind never arrived.

## Accomplishments

Milestone 1 runs end to end. The package installs, the CLI defaults to live data, and tests replay recorded fixtures so CI never hits the network. A live Open-Meteo call returns current 10 m wind, 850 hPa wind, and boundary-layer height without a key. FIRMS, AirNow, and OpenAQ run the same path when their keys are set; without those keys the CLI says which variable is missing and still prints the wind.

Quality checks remove low-confidence detections, bad coordinates, negative FRP, and MODIS hotspots that are not vegetation fires. Clusters are FRP-weighted. The hourly score responds to upwind versus downwind, to FRP, and to a shallower boundary layer. Short wind gaps are interpolated and flagged. Longer gaps and a dead feed are called out. The PM2.5 breakpoint table matches the 2024 EPA cut points, including values above AQI 500.

## What we learned

The useful product at this stage is not a chart of raw hotspots. It is a sentence about whether those hotspots are lined up with the wind that will blow over a particular person, plus the monitor reading they can compare it with.

A uniform wind at one point is enough to make upwind and downwind differ sharply, and it is not enough to claim a concentration. Keeping that distinction in the output matters more than a more complicated formula.

Feeds that look like one API are several products. VIIRS NOAA-21 can be up while S-NPP is not, and an empty Landsat file over the ocean is a success, not an outage. The code has to tell those apart.

## What's next

Milestone 2 trains a PM2.5 model on the smoke score plus monitor observations, with a time-based split and a persistence baseline. `predict_pm25` raises until that exists. The breakpoint function stays the way a concentration becomes an AQI.

The MCP server is in place: an agent calls `forecast_smoke` and `pm25_to_aqi` over stdio or streamable HTTP. Milestone 2 is still the trained PM2.5 model. What remains of milestone 3 is a map and an hourly chart that call those same tools. See `docs/roadmap.md` and `docs/methodology.md`.

## How Grok Bot helped build it

Placeholder for the team: describe how the Grok bot was used while building WildFireBot.

## How to run it

Requires Python 3.11 or newer (`python3 --version`). On macOS the system `python3` may be older; use `brew install python@3.12` or `uv python install 3.12`.

```bash
git clone https://github.com/nycbagel/WildFireBot && cd WildFireBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Open-Meteo needs no key. The others do:

- `FIRMS_MAP_KEY` from https://firms.modaps.eosdis.nasa.gov/api/area/
- `AIRNOW_API_KEY` from https://docs.airnowapi.org/account/request/
- `OPENAQ_API_KEY` from https://docs.openaq.org/using-the-api/quick-start (optional; without it OpenAQ is skipped and noted under `notices`)

The CLI reads `.env` without overriding variables that are already set.

```bash
aqi-smoke forecast --lat 37.8 --lon -122.3 --hours 48
aqi-smoke aqi --lat 37.8 --lon -122.3            # plume AQI forecast, add --json for tool output
```

`wildfirebot forecast` is the same command. The default is live data. With only Open-Meteo reachable you still get the wind summary, and `DEGRADED MODE` names the fire and monitor feeds that were skipped. Responses are cached under `~/.cache/wildfirebot`. `--refresh` skips a fresh cache. A stale cache is used only when the live feed fails, and the age is printed.

Tests and lint:

```bash
ruff check .
ruff format --check .
pytest
```

`--fixture-dir tests/fixtures/cli` replays recorded responses for development. It is not the default, and the banner says so. Do not commit raw downloads or `.env`.

This is an estimate of relative smoke exposure from satellite detections and forecast wind. It is not a health advisory and not a substitute for AirNow or local guidance.

## Use with your AI agent

The MCP server is `wildfirebot-mcp` (stdio by default). There are two ways to run it.

**Option A: from your local clone** (after the "How to run it" steps above). Use absolute paths, because the agent app does not start the server in the repo folder:

```json
{
  "mcpServers": {
    "wildfirebot": {
      "command": "/ABSOLUTE/PATH/TO/WildFireBot/.venv/bin/wildfirebot-mcp",
      "env": {
        "FIRMS_MAP_KEY": "your-firms-map-key",
        "AIRNOW_API_KEY": "your-airnow-key"
      }
    }
  }
}
```

On Windows the command is `C:\path\to\WildFireBot\.venv\Scripts\wildfirebot-mcp.exe`.

**Option B: no clone, with uv.** Install uv first (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`), then:

```bash
uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

Add `--refresh` after `uvx` to pick up new commits. A Claude Desktop or Cursor config for this option:

```json
{
  "mcpServers": {
    "wildfirebot": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/nycbagel/WildFireBot",
        "wildfirebot-mcp"
      ],
      "env": {
        "FIRMS_MAP_KEY": "your-firms-map-key",
        "AIRNOW_API_KEY": "your-airnow-key",
        "OPENAQ_API_KEY": "your-openaq-key"
      }
    }
  }
}
```

Where the config goes:

- Cursor: `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects), then enable `wildfirebot` under Settings → MCP.
- Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows, then restart Claude Desktop.

Put the keys in the `env` block. The server only reads `.env` from its working directory, which agent apps usually do not set to the repo, so a `.env` in the clone is not picked up there.

Open-Meteo needs no key. If a key is missing, the tool result says so under `notices` and `degraded` instead of crashing. Every forecast includes a disclaimer: the score is a relative exposure index, not an official AQI and not a health advisory.

To host it at a URL, use streamable HTTP. The MCP endpoint is `http://127.0.0.1:8000/mcp`:

```bash
uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp --http --port 8000
```

`--host` defaults to `127.0.0.1`. The tools are `forecast_smoke` (`lat`, `lon`, `hours` default 48, `radius_km` default 250, `days` default 2, `lookback_hours` default 12), `forecast_smoke_aqi` (Gaussian plume PM2.5 and EPA AQI), and `pm25_to_aqi` (`concentration`).

## Hosted server (no API keys for users)

Run the MCP server once with the operator's keys, and everyone else connects by URL. The keys stay in the host's environment and are never sent to clients.

**Environment variables**

| Variable | Purpose |
| --- | --- |
| `FIRMS_MAP_KEY`, `AIRNOW_API_KEY` | Operator's keys (set as host secrets, never commit them) |
| `PORT` | Set by the host. When present the server serves HTTP on `0.0.0.0:$PORT` at `/mcp` |
| `WFB_ACCESS_TOKEN` | Optional. If set, clients must send `Authorization: Bearer <token>`. If unset the server is open |
| `WFB_RATE_LIMIT` / `WFB_RATE_WINDOW_S` | Requests per client IP per window (default 60 per 60 s, `0` disables) |

`GET /health` returns `{"status":"ok"}` without auth, for host health checks.

**Deploy on Render**: create a Render account, choose New > Blueprint, and point it at this repo (`render.yaml`). Paste `FIRMS_MAP_KEY`, `AIRNOW_API_KEY`, and `WFB_ACCESS_TOKEN` when prompted. The endpoint is `https://<service>.onrender.com/mcp`. The free tier sleeps when idle, so the first call can take about a minute.

**Deploy on Fly.io**: `fly launch --copy-config --no-deploy`, then `fly secrets set FIRMS_MAP_KEY=... AIRNOW_API_KEY=... WFB_ACCESS_TOKEN=...`, then `fly deploy`.

**Any Docker host**: `docker build -t wildfirebot-mcp .` then `docker run -p 8000:8000 -e PORT=8000 -e FIRMS_MAP_KEY -e AIRNOW_API_KEY -e WFB_ACCESS_TOKEN wildfirebot-mcp`.

**Connect as a user.** Cursor (`~/.cursor/mcp.json`) or Claude Code:

```json
{
  "mcpServers": {
    "wildfirebot": {
      "url": "https://YOUR-HOST/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

Claude Desktop only launches local servers from its config file, so bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "wildfirebot": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-HOST/mcp", "--header", "Authorization: Bearer ${WFB_TOKEN}"],
      "env": { "WFB_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```
