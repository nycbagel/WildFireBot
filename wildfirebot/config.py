"""Runtime settings. API keys come from the environment, never from the repo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NWS_USER_AGENT = "WildFireBot (set NWS_USER_AGENT to a contact email)"

# Package root is the repository root when this file lives in wildfirebot/.
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    """Keys and the NWS User-Agent.

    ``repr`` hides key values so a traceback cannot print them.
    """

    firms_map_key: str
    airnow_api_key: str
    nws_user_agent: str

    def __repr__(self) -> str:
        return (
            "Settings("
            f"firms_map_key={'set' if self.firms_map_key else 'missing'}, "
            f"airnow_api_key={'set' if self.airnow_api_key else 'missing'}, "
            f"nws_user_agent={self.nws_user_agent!r})"
        )


def _dotenv_values(path: Path) -> dict[str, str | None]:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    if not path.is_file():
        return {}
    loaded = dotenv_values(path)
    return dict(loaded)


def _pick(name: str, files: list[dict[str, str | None]], default: str = "") -> str:
    """Environment variables win. Then the cwd ``.env``, then the repo ``.env``.

    A blank value counts as set, so an empty key in the environment disables
    a key that happens to be sitting in a file.
    """

    if name in os.environ:
        return os.environ[name].strip()
    for mapping in files:
        if name in mapping and mapping[name] is not None:
            return str(mapping[name]).strip()
    return default


def load_settings() -> Settings:
    """Load ``FIRMS_MAP_KEY``, ``AIRNOW_API_KEY``, and ``NWS_USER_AGENT``."""

    files = [
        _dotenv_values(Path.cwd() / ".env"),
        _dotenv_values(_REPO_ROOT / ".env"),
    ]
    user_agent = _pick("NWS_USER_AGENT", files, default=DEFAULT_NWS_USER_AGENT)
    return Settings(
        firms_map_key=_pick("FIRMS_MAP_KEY", files),
        airnow_api_key=_pick("AIRNOW_API_KEY", files),
        nws_user_agent=user_agent or DEFAULT_NWS_USER_AGENT,
    )
