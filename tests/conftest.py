import urllib.request

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Unit tests stay on fixtures. A test that checks the live default overrides this."""

    def blocked(*args, **kwargs):
        raise AssertionError("tests must not call live network APIs")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
