"""Small JSON state file: since_id, handled mention ids, and the post-rate counters."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

MAX_HANDLED = 5000


@dataclass
class State:
    since_id: str | None = None
    bot_user_id: str | None = None
    handled: dict[str, str] = field(default_factory=dict)  # mention id -> outcome
    failures: dict[str, int] = field(default_factory=dict)
    posts: list[str] = field(default_factory=list)  # ISO times of replies
    authors: dict[str, list[str]] = field(default_factory=dict)  # author id -> ISO times

    def is_handled(self, mention_id: str) -> bool:
        return mention_id in self.handled

    def mark(self, mention_id: str, outcome: str) -> None:
        self.handled[mention_id] = outcome
        if len(self.handled) > MAX_HANDLED:
            for key in sorted(self.handled, key=int)[: len(self.handled) - MAX_HANDLED]:
                del self.handled[key]

    def advance(self, mention_id: str) -> None:
        if self.since_id is None or int(mention_id) > int(self.since_id):
            self.since_id = mention_id

    def record_post(self, author_id: str, now: datetime) -> None:
        stamp = now.isoformat()
        self.posts.append(stamp)
        self.authors.setdefault(author_id, []).append(stamp)

    def _recent(self, stamps: list[str], now: datetime, span: timedelta) -> int:
        return sum(1 for s in stamps if now - datetime.fromisoformat(s) < span)

    def posts_last_hour(self, now: datetime) -> int:
        return self._recent(self.posts, now, timedelta(hours=1))

    def author_posts_last_day(self, author_id: str, now: datetime) -> int:
        return self._recent(self.authors.get(author_id, []), now, timedelta(days=1))

    def prune(self, now: datetime) -> None:
        self.posts = [s for s in self.posts if now - datetime.fromisoformat(s) < timedelta(days=1)]
        for key in list(self.authors):
            kept = [
                s for s in self.authors[key] if now - datetime.fromisoformat(s) < timedelta(days=1)
            ]
            if kept:
                self.authors[key] = kept
            else:
                del self.authors[key]


def load_state(path: Path) -> State:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    if not isinstance(raw, dict):
        return State()
    return State(
        since_id=raw.get("since_id"),
        bot_user_id=raw.get("bot_user_id"),
        handled=dict(raw.get("handled") or {}),
        failures=dict(raw.get("failures") or {}),
        posts=list(raw.get("posts") or []),
        authors={k: list(v) for k, v in (raw.get("authors") or {}).items()},
    )


def save_state(path: Path, state: State) -> None:
    """Write atomically so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(state.__dict__, out, indent=1, sort_keys=True)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
