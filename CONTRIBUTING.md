# Contributing

WildFireBot is early. Milestone 1 is the data clients, the smoke-exposure score, and the CLI. The same forecast function is what a future MCP server will call.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Put keys in `.env` or the environment. Do not commit `.env`, the cache, or downloaded detections.

```bash
ruff check .
ruff format --check .
pytest
pre-commit install   # optional
```

Tests use recorded responses under `tests/fixtures/` and block live HTTP. The CLI defaults to live feeds. `--fixture-dir` is only for tests and local debugging; the banner says `DATA MODE: fixtures (not live)` when it is on.

## Adding a data source

Implement `fetch(self, bbox, time_range) -> SourceResult` on a client in `src/wildfirebot/sources/`. `SourceResult.data` is a dataframe. Put outages, stale cache, and partial results in `notices`, and set `degraded` when the caller should not treat the table as complete.

Record a small fixture. Point a test at it with `ScriptedTransport` in `tests/support.py`. Do not call the live API from pytest.

## Adding behavior an agent should be able to call

Put it in `src/wildfirebot/tools.py` as a tool with a JSON schema and a `call_tool` branch. The CLI and the milestone 3 MCP server both go through that module. Do not bury new forecast logic only in `cli.py`.

## Secrets

`FIRMS_MAP_KEY`, `AIRNOW_API_KEY`, and `OPENAQ_API_KEY` stay in the environment. Error text redacts keys in FIRMS paths and `API_KEY` query parameters. If you add a header-based key, keep it out of exception messages.
