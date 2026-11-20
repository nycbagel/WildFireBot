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

Dina Safreno and Dani Abay described the idea to Grok Bot: predict AQI for a location from NASA FIRMS fire detections and forecast winds. Dina and Dani made the product decisions (the idea, the California focus, the MCP direction, and the Most Technically Complex track) and reviewed and merged the pull requests. Grok Bot did the research, the project brief, and the agent work below. It can also call WildFireBot itself over MCP. See [Grok Bot](#grok-bot).

**Planning.** Grok Bot researched the data sources: the NASA FIRMS area API for MODIS, VIIRS NOAA-20, VIIRS NOAA-21, and Landsat; Open-Meteo winds at 10 m and about 850 hPa plus boundary-layer height; AirNow and OpenAQ PM2.5; and the EPA PM2.5-to-AQI breakpoints. It proposed the pipeline: FRP-weighted fire clusters, smoke transport along forecast winds, a PM2.5 model, then AQI.

**Open-source structure.** Grok Bot proposed the repository layout and the milestones, and wrote the brief that encodes the track's criteria: data depth, real current data, and robustness (satellite-source fallback, low-confidence filtering, retries, a cache with staleness warnings, missing-wind-hour flags, and live data by default).

**Code.** Grok Bot connected GitHub and launched a Cursor cloud agent on this repository, steering it across these pull requests:

- Milestone 1: https://github.com/nycbagel/WildFireBot/pull/1
- A CI test-import fix after a failing lint-and-test check, folded into main: https://github.com/nycbagel/WildFireBot/pull/2
- The real MCP server: https://github.com/nycbagel/WildFireBot/pull/5, re-opened as https://github.com/nycbagel/WildFireBot/pull/8 into main when #5 was stuck targeting a closed stacked branch
- Per-client MCP install docs: https://github.com/nycbagel/WildFireBot/pull/9

**Testing.** Grok Bot installed the package fresh on its own computer, ran live CLI forecasts for the Bay Area and Los Angeles, and confirmed degraded mode when keys were missing. It found that the MCP server was still a placeholder and had it built for real, then verified the tools with a real MCP client: `pm25_to_aqi(55)` returned AQI 149, Unhealthy for Sensitive Groups.

The redacted session export is in [grok history](grok%20history/).

## How to run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Open-Meteo needs no key. The others do:

- `FIRMS_MAP_KEY` from https://firms.modaps.eosdis.nasa.gov/api/area/
- `AIRNOW_API_KEY` from https://docs.airnowapi.org/account/request/
- `OPENAQ_API_KEY` from https://docs.openaq.org/using-the-api/quick-start

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

The MCP server is `wildfirebot-mcp`. Stdio is the default. No clone is required once `uv` is installed ([install uv](https://docs.astral.sh/uv/getting-started/installation/)):

```bash
uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

Keys:

- `FIRMS_MAP_KEY` is required for fire detections. Sign up at https://firms.modaps.eosdis.nasa.gov/api/area/
- `AIRNOW_API_KEY` is optional (nearest monitors). Request one at https://docs.airnowapi.org/account/request/
- `OPENAQ_API_KEY` is optional (open measurements). See https://docs.openaq.org/using-the-api/quick-start

Open-Meteo needs no key. If a key is missing, the tool result says so under `notices` and `degraded` instead of crashing. Every forecast includes a disclaimer: the score is a relative exposure index, not an official AQI and not a health advisory.

The tools are `forecast_smoke` (`lat`, `lon`, `hours` default 48, `radius_km` default 250, `days` default 2, `lookback_hours` default 12), `forecast_smoke_aqi` (Gaussian plume PM2.5 and EPA AQI), and `pm25_to_aqi` (`concentration`).

A prompt to try in any of the clients below: "What will the smoke AQI be in Oakland over the next 24 hours?"

### Claude Desktop, Cursor, and other JSON configs

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

Needs `uv` on `PATH`. `FIRMS_MAP_KEY` is the fire feed. The AirNow and OpenAQ keys are optional.

### Claude Code

Requires the Claude Code CLI and `uv`. `FIRMS_MAP_KEY` is required for fire data. Add `AIRNOW_API_KEY` and `OPENAQ_API_KEY` the same way if you have them.

Local scope is the default: private to you, and only the project you are in. `-e` is the short form of `--env`. Everything after `--` is the server command, so Claude Code does not treat `uvx` flags as its own:

```bash
claude mcp add wildfirebot -e FIRMS_MAP_KEY=your-firms-map-key -- uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

Optional keys are more `-e` flags, before `--`:

```bash
claude mcp add wildfirebot \
  -e FIRMS_MAP_KEY=your-firms-map-key \
  -e AIRNOW_API_KEY=your-airnow-key \
  -e OPENAQ_API_KEY=your-openaq-key \
  -- uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

`--scope` (`-s`) chooses where that entry is stored:

| Scope | Flag | Where it loads | Stored in |
| --- | --- | --- | --- |
| Local (default) | `--scope local` | This project, only you | `~/.claude.json` (under this project's path) |
| User | `--scope user` | All of your projects, only you | `~/.claude.json` (top-level `mcpServers`) |
| Project | `--scope project` | This project, shared with the team | `.mcp.json` in the project root |

User scope, still private, for every project:

```bash
claude mcp add --scope user wildfirebot -e FIRMS_MAP_KEY=your-firms-map-key -- uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

Project scope writes `.mcp.json` and is meant to be committed. Do not put a real key in that command. Add the command without `-e`, then put the key only in a copy that stays off git, or keep the key in local or user scope.

The same project file, written by hand:

```json
{
  "mcpServers": {
    "wildfirebot": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/nycbagel/WildFireBot",
        "wildfirebot-mcp"
      ],
      "env": {
        "FIRMS_MAP_KEY": "your-firms-map-key"
      }
    }
  }
}
```

Claude Code reads `.mcp.json` when a session starts. The first time it sees a project server it asks you to approve it, because a cloned repo must not launch a process without consent. If you dismissed that prompt, run `claude mcp reset-project-choices` and start Claude Code again.

Check the connection from the shell. `Added ...` only means the entry was saved. The first `claude mcp list` can show `✘ Failed to connect` while `uvx` downloads the package; run it again and look for `✔ Connected`. A project server you have not approved yet shows `⏸ Pending approval (run claude to approve)`.

```bash
claude mcp list
claude mcp get wildfirebot
```

Inside a Claude Code session, `/mcp` shows the same status and the tool list. Restart the session after you edit `.mcp.json`.

Try: "What will the smoke AQI be in Oakland over the next 24 hours?"

### OpenAI Codex (CLI and IDE)

Requires the Codex CLI and `uv`. The CLI, the IDE extension, and the ChatGPT desktop app share `~/.codex/config.toml`. `FIRMS_MAP_KEY` is required for fire data. Repeat `--env` for each optional key. The official flag is `--env`, not `-e`.

```bash
codex mcp add wildfirebot --env FIRMS_MAP_KEY=your-firms-map-key -- uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

With the optional monitor keys:

```bash
codex mcp add wildfirebot \
  --env FIRMS_MAP_KEY=your-firms-map-key \
  --env AIRNOW_API_KEY=your-airnow-key \
  --env OPENAQ_API_KEY=your-openaq-key \
  -- uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp
```

The same entry in `~/.codex/config.toml` (or a project-scoped `.codex/config.toml`, which Codex loads only after you trust the project):

```toml
[mcp_servers.wildfirebot]
command = "uvx"
args = ["--from", "git+https://github.com/nycbagel/WildFireBot", "wildfirebot-mcp"]

[mcp_servers.wildfirebot.env]
FIRMS_MAP_KEY = "your-firms-map-key"
AIRNOW_API_KEY = "your-airnow-key"
OPENAQ_API_KEY = "your-openaq-key"
```

In the IDE extension: open the gear menu, select MCP servers, select Add server, choose STDIO, and enter the same command. Save, then select Restart extension.

Verify from the shell with `codex mcp list`. In the Codex terminal UI, `/mcp` lists the active servers.

Try: "What will the smoke AQI be in Oakland over the next 24 hours?"

### Grok Bot

Grok Bot adds a custom MCP server when you ask it in chat. It runs the server on its own computer. Paste this into a Grok Bot chat:

> Add a custom MCP server named wildfirebot that runs `uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp`, with env FIRMS_MAP_KEY set to my key.

`uv` has to be available on the computer Grok Bot uses. `FIRMS_MAP_KEY` is required for fire data. Mention `AIRNOW_API_KEY` and `OPENAQ_API_KEY` in the same message if you want those monitors. Grok Bot asks for the key securely rather than taking it in plain chat, confirms, then starts the server.

If you are already hosting the server, give Grok Bot that HTTPS URL instead of the `uvx` command. Run `wildfirebot-mcp --http` (see below) and put HTTPS in front of it before sharing the URL.

Try: "What will the smoke AQI be in Oakland over the next 24 hours?"

### Host it at a URL

Streamable HTTP serves the same tools. The local MCP endpoint is `http://127.0.0.1:8000/mcp`:

```bash
uvx --from git+https://github.com/nycbagel/WildFireBot wildfirebot-mcp -- --http --port 8000
```

`--host` defaults to `127.0.0.1`. The `--` before `--http` is for `uvx`, so it passes the flag through to `wildfirebot-mcp`.

Claude Code adds that URL with `claude mcp add --transport http wildfirebot https://your-host.example/mcp`. Codex adds it with `codex mcp add wildfirebot --url https://your-host.example/mcp`, or with `url = "https://your-host.example/mcp"` under `[mcp_servers.wildfirebot]` instead of `command` and `args`.
