"""No test reaches the network. The TypeSafe seams raise unless a test replaces them,
so a key in `src/strong-loop/.env` cannot turn the suite into live API calls."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def blocked(*args, **kwargs):
        raise RuntimeError("network disabled in tests; monkeypatch the seam")

    import loop.suggest as suggest

    monkeypatch.setattr(suggest, "_ask", blocked)
    monkeypatch.setattr(suggest, "_ask_nouls", blocked)

    def blocked_sync(*args, **kwargs):
        raise RuntimeError("network disabled in tests; monkeypatch the seam")

    monkeypatch.setattr(suggest, "_ask_sync", blocked_sync)
    suggest._SCREEN_CACHE.clear()
    yield
