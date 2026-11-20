"""The public repo must not carry API keys."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_covers_env_and_the_example_is_blank():
    ignored = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in ignored
    assert not (ROOT / ".env").exists()

    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    assert values["FIRMS_MAP_KEY"] == ""
    assert values["AIRNOW_API_KEY"] == ""
    assert values["NWS_USER_AGENT"].startswith("WildFireBot")
