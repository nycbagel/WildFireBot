"""Poll mentions, answer each at most once, and only because someone tagged the bot."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wildfirebot.cli import load_dotenv
from wildfirebot.tools import Runtime, build_runtime, forecast_smoke_aqi
from wildfirebot.xbot.geocode import Place, geocode
from wildfirebot.xbot.parse import When, parse_mention
from wildfirebot.xbot.reply import (
    ASK_PLACE,
    FORECAST_REACH_H,
    Decision,
    Thresholds,
    compose,
    decide,
    window_horizons,
)
from wildfirebot.xbot.state import State, load_state, save_state
from wildfirebot.xbot.xapi import Credentials, XClient, XError, XRateLimited

MAX_FAILURES = 3
Forecaster = Callable[[dict, Runtime | None], dict]


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    dry_run: bool = True
    reply_all_clear: bool = True
    thresholds: Thresholds = Thresholds()
    max_per_hour: int = 20
    max_per_author_day: int = 3
    interval_s: float = 120.0
    state_path: Path = Path(".xbot_state.json")
    bot_user_id: str = ""
    bot_username: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        dry = _flag(e.get("WFB_X_DRY_RUN"), True)
        state = Path(e.get("WFB_X_STATE_FILE") or ".xbot_state.json")
        if dry:  # a dry run never shares (or spends) the live state
            state = state.with_name(state.name + ".dryrun")
        return cls(
            dry_run=dry,
            reply_all_clear=_flag(e.get("WFB_X_REPLY_ALL_CLEAR"), True),
            thresholds=Thresholds(
                aqi=int(e.get("WFB_X_AQI_THRESHOLD") or 101),
                fire_km=float(e.get("WFB_X_FIRE_KM") or 50),
                min_fire_frp_mw=float(e.get("WFB_X_MIN_FIRE_FRP_MW") or 10),
            ),
            max_per_hour=int(e.get("WFB_X_MAX_REPLIES_PER_HOUR") or 20),
            max_per_author_day=int(e.get("WFB_X_MAX_REPLIES_PER_AUTHOR_PER_DAY") or 3),
            interval_s=float(e.get("WFB_X_INTERVAL_S") or 120),
            state_path=state,
            bot_user_id=e.get("X_BOT_USER_ID", ""),
            bot_username=e.get("X_BOT_USERNAME", ""),
        )


@dataclass(frozen=True)
class Answer:
    text: str | None  # None: say nothing
    outcome: str
    place: Place | None = None
    decision: Decision | None = None


def answer_text(
    mention: str,
    parent: str | None,
    *,
    now: datetime,
    settings: Settings,
    runtime: Runtime | None = None,
    geocoder: Callable[[str], Place | None] | None = None,
    forecaster: Forecaster = forecast_smoke_aqi,
) -> Answer:
    """Turn a mention (and its parent post) into a reply. No network unless injected."""
    parsed = parse_mention(mention, parent, now)
    lookup = geocoder or (lambda name: geocode((runtime or build_runtime()).http, name))
    place = next((found for name in parsed.candidates[:3] if (found := lookup(name))), None)
    if place is None:
        return Answer(ASK_PLACE, "asked-for-place")
    when = parsed.when or When(now, now, f"next {FORECAST_REACH_H} h", (0, 0))
    start_h = max(0.0, (when.start - now).total_seconds() / 3600)
    end_h = (
        float(FORECAST_REACH_H)
        if parsed.when is None
        else max(start_h, (when.end - now).total_seconds() / 3600)
    )
    limits = settings.thresholds
    horizons = window_horizons(start_h, end_h) or [0]
    forecast = forecaster(
        {
            "lat": place.latitude,
            "lon": place.longitude,
            "radius_km": max(150.0, limits.fire_km * 1.5),
            "horizons": horizons,
            "now": now.isoformat(),
        },
        runtime,
    )
    decision = decide(forecast, start_h, end_h, limits)
    if decision.kind == "all_clear" and not decision.fire_feed_ok:
        # never say "all clear" when the fire feed did not answer; the caller retries
        raise RuntimeError("fire detections unavailable")
    text = compose(place.label, when.label, decision, limits)
    if decision.kind == "all_clear" and not settings.reply_all_clear:
        return Answer(None, "skipped-all-clear", place, decision)
    return Answer(text, decision.kind, place, decision)


def _parent_text(post: dict, referenced: dict[str, dict]) -> str | None:
    for ref in post.get("referenced_tweets") or []:
        if ref.get("type") == "replied_to":
            parent = referenced.get(str(ref.get("id")))
            return None if parent is None else parent.get("text")
    return None


def _is_retweet(post: dict) -> bool:
    return any(r.get("type") == "retweeted" for r in post.get("referenced_tweets") or [])


@dataclass
class RunResult:
    seen: int = 0
    replied: int = 0
    skipped: int = 0
    stopped: str | None = None


def run_once(
    client: XClient,
    state: State,
    settings: Settings,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    runtime: Runtime | None = None,
    geocoder: Callable[[str], Place | None] | None = None,
    forecaster: Forecaster = forecast_smoke_aqi,
    out: Callable[[str], None] = print,
) -> RunResult:
    result = RunResult()
    if not state.bot_user_id:
        state.bot_user_id = settings.bot_user_id or client.user_id(settings.bot_username)
    posts, referenced = client.mentions(state.bot_user_id, state.since_id)
    for post in posts:
        post_id = str(post["id"])
        moment = now()
        state.prune(moment)
        result.seen += 1
        author = str(post.get("author_id", ""))
        if state.is_handled(post_id):
            state.advance(post_id)
            continue
        if author == state.bot_user_id or _is_retweet(post):
            state.mark(post_id, "ignored")
            state.advance(post_id)
            result.skipped += 1
            continue
        if state.posts_last_hour(moment) >= settings.max_per_hour:
            result.stopped = "hourly reply limit reached; the rest wait for the next poll"
            break  # since_id stays put so these are read again later
        if state.author_posts_last_day(author, moment) >= settings.max_per_author_day:
            state.mark(post_id, "skipped-author-limit")
            state.advance(post_id)
            result.skipped += 1
            continue
        try:
            answer = answer_text(
                post.get("text", ""),
                _parent_text(post, referenced),
                now=moment,
                settings=settings,
                runtime=runtime,
                geocoder=geocoder,
                forecaster=forecaster,
            )
        except Exception as exc:  # noqa: BLE001 - one bad mention must not stop the rest
            failures = state.failures.get(post_id, 0) + 1
            state.failures[post_id] = failures
            out(f"mention {post_id}: forecast failed ({exc.__class__.__name__}), try {failures}")
            if failures >= MAX_FAILURES:
                state.mark(post_id, "skipped-error")
                state.advance(post_id)
            else:
                result.stopped = "a forecast failed; it will be retried at the next poll"
                break
            continue
        if answer.text is None:
            state.mark(post_id, answer.outcome)
            state.advance(post_id)
            result.skipped += 1
            continue
        state.mark(post_id, "replied" if not settings.dry_run else "dry-run")
        state.record_post(author, moment)
        state.advance(post_id)
        save_state(settings.state_path, state)  # before posting: never reply twice
        if settings.dry_run:
            out(f"[dry run] reply to mention {post_id} ({answer.outcome}): {answer.text}")
        else:
            try:
                client.reply(answer.text, post_id)
            except XRateLimited as exc:
                state.handled.pop(post_id, None)
                state.posts.pop()
                state.authors[author].pop()
                result.stopped = str(exc)
                break
            except XError as exc:
                if exc.status is not None:  # X refused it, so nothing was posted
                    state.handled.pop(post_id, None)
                    state.posts.pop()
                    state.authors[author].pop()
                    state.mark(post_id, f"skipped-post-failed-{exc.status}")
                out(f"mention {post_id}: post failed ({exc})")
                continue
        result.replied += 1
    save_state(settings.state_path, state)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wildfirebot.xbot",
        description="Answer @mentions with a wildfire and smoke check. Dry run unless WFB_X_DRY_RUN=0.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll once and exit")
    mode.add_argument("--loop", action="store_true", help="Poll forever (default)")
    mode.add_argument("--text", help="Answer this text locally, with no X access")
    parser.add_argument("--parent", help="With --text: the post being replied to")
    parser.add_argument("--interval", type=float, help="Seconds between polls in loop mode")
    parser.add_argument("--env-file", default=".env", help="Optional dotenv path")
    args = parser.parse_args(argv)
    load_dotenv(Path(args.env_file))
    settings = Settings.from_env()
    if args.text is not None:
        answer = answer_text(args.text, args.parent, now=datetime.now(UTC), settings=settings)
        print(answer.text or f"(no reply: {answer.outcome})")
        return 0
    credentials = Credentials.from_env(dict(os.environ))
    if not settings.dry_run and not credentials.can_post:
        print(
            "WFB_X_DRY_RUN=0 needs X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET",
            file=sys.stderr,
        )
        return 2
    if not credentials.bearer or not (settings.bot_user_id or settings.bot_username):
        print("Set X_BEARER_TOKEN and X_BOT_USER_ID (or X_BOT_USERNAME)", file=sys.stderr)
        return 2
    client = XClient(credentials)
    state = load_state(settings.state_path)
    runtime = build_runtime()
    print(f"WildFireBot X bot: {'DRY RUN (nothing is posted)' if settings.dry_run else 'LIVE'}")
    interval = args.interval or settings.interval_s
    while True:
        try:
            result = run_once(client, state, settings, runtime=runtime)
            print(
                f"mentions {result.seen}, replies {result.replied}, skipped {result.skipped}"
                + (f", stopped: {result.stopped}" if result.stopped else "")
            )
        except XError as exc:
            print(f"X error: {exc}", file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(interval)
