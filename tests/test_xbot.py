import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest

from tests.support import ScriptedTransport
from wildfirebot.http import HttpClient
from wildfirebot.xbot.bot import Settings, answer_text, run_once
from wildfirebot.xbot.geocode import Place, geocode
from wildfirebot.xbot.parse import parse_mention
from wildfirebot.xbot.reply import MAX_CHARS, Thresholds, compose, decide, window_horizons
from wildfirebot.xbot.state import State, load_state, save_state
from wildfirebot.xbot.xapi import (
    Credentials,
    XClient,
    XError,
    XRateLimited,
    oauth1_header,
)

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)  # a Saturday
YOSEMITE = Place("Yosemite National Park", 37.848, -119.557, "California, US")
STABLE = (
    "stable night air keeps the smoke plume aloft; fire PM2.5 here may be underestimated, "
    "as smoke can pool in valleys at night"
)


def forecast(
    aqi=52, fire=None, notes=(), fire_feed=True, degraded=False, bad_hours=(24, 36, 42, 48)
):
    hours = [0, 3, 6, 9, 12, 18, 24, 30, 36, 42, 48]
    rows = [
        {
            "horizon_hours": h,
            "aqi": aqi if h in bad_hours else 40,
            "category": "Unhealthy for Sensitive Groups" if aqi >= 101 else "Moderate",
            "notes": list(notes),
        }
        for h in hours
    ]
    cluster = []
    if fire:
        cluster = [
            {
                "distance_km": fire[0],
                "frp_mw": fire[1],
                "direction_from_target": "NNW",
            }
        ]
    return {
        "horizons": rows,
        "clusters": cluster,
        "degraded": degraded,
        "notices": ["FIRMS detections from VIIRS_NOAA21_NRT."]
        if fire_feed
        else ["FIRMS_MAP_KEY is not set"],
    }


def settings(tmp_path, **kw):
    base = Settings(dry_run=True, state_path=tmp_path / "state.json")
    return Settings(**{**base.__dict__, **kw})


def ask(mention, parent=None, fc=None, place=YOSEMITE, **kw):
    return answer_text(
        mention,
        parent,
        now=NOW,
        settings=kw.pop("cfg", Settings()),
        geocoder=lambda name: place,
        forecaster=lambda args, runtime: fc or forecast(),
        **kw,
    )


# ---------------------------------------------------------------- parsing


def test_parses_yosemite_in_two_days_from_the_parent_post():
    parsed = parse_mention(
        "@WildFireBot is this ok?", "excited for my trip to yosemite in 2 days", NOW
    )
    assert parsed.candidates[0].lower() == "yosemite"
    assert parsed.when.label == "in 2 days"
    assert parsed.when.start == NOW + timedelta(days=2) - timedelta(hours=12)


def test_parses_this_weekend_in_the_mention_itself():
    parsed = parse_mention("@WildFireBot Yosemite this weekend?", None, NOW)
    assert parsed.candidates == ["Yosemite"]
    assert parsed.when.label == "this weekend"
    assert parsed.when.start == NOW  # it is already Saturday
    assert parsed.when.end == datetime(2026, 11, 23, tzinfo=UTC)


def test_parses_explicit_dates_and_ranges():
    parsed = parse_mention("@WildFireBot", "Going to Lake Tahoe Nov 25-27 with friends!", NOW)
    assert parsed.candidates == ["Lake Tahoe"]
    assert parsed.when.label == "Nov 25-27"
    assert parsed.when.start == datetime(2026, 11, 25, tzinfo=UTC)
    assert parsed.when.end == datetime(2026, 11, 28, tzinfo=UTC)
    assert parse_mention("@x", "trip to Paris 12/3", NOW).when.label == "Dec 3"
    assert parse_mention("@x", "visiting Bend on Jan 2", NOW).when.start.year == 2027


def test_no_place_in_the_text():
    for text in ("@WildFireBot hello", "@WildFireBot thoughts?", "@WildFireBot excited to see you"):
        assert parse_mention(text, "nice day", NOW).candidates == []


def test_lowercase_place_after_a_travel_word_and_park_suffix():
    assert parse_mention(
        "@x", "road trip to joshua tree national park on saturday", NOW
    ).candidates == ["joshua tree national park"]


# ---------------------------------------------------------------- geocoding


def _http(transport):
    return HttpClient(transport, sleep=lambda _s: None, clock=lambda: NOW)


def test_geocoder_prefers_the_park_over_a_village_of_the_same_name():
    village = {"name": "Yosemite", "latitude": 37.3, "longitude": -84.8, "feature_code": "PPL"}
    park = {
        "name": "Yosemite National Park",
        "latitude": 37.85,
        "longitude": -119.56,
        "feature_code": "PRK",
        "admin1": "California",
        "country_code": "US",
    }
    transport = ScriptedTransport()
    transport.when("name=Yosemite+National+Park", 200, json.dumps({"results": [park]}))
    transport.when("name=Yosemite&", 200, json.dumps({"results": [village]}))
    place = geocode(_http(transport), "Yosemite")
    assert place.name == "Yosemite National Park"
    assert place.latitude == pytest.approx(37.85)
    empty = ScriptedTransport().when("geocoding-api", 200, "{}")
    assert geocode(_http(empty), "Nowhereville") is None


# ---------------------------------------------------------------- decision and text


def test_danger_on_high_forecast_aqi_in_the_window():
    answer = ask("@b", "trip to Yosemite in 2 days", fc=forecast(aqi=140, fire=(120, 80)))
    assert answer.outcome == "danger"
    assert "Heads-up for Yosemite National Park (in 2 days)" in answer.text
    assert "AQI up to 140 (Unhealthy for Sensitive Groups)" in answer.text


def test_bad_air_outside_the_trip_window_is_ignored():
    soon_only = forecast(aqi=150, bad_hours=(0, 3))
    assert ask("@b", "trip to Yosemite in 2 days", fc=soon_only).outcome == "all_clear"
    assert ask("@b Yosemite tonight", fc=soon_only).outcome == "danger"


def test_danger_on_a_close_fire_even_with_good_air():
    answer = ask("@b Yosemite this weekend?", fc=forecast(aqi=52, fire=(5.9, 62.3)))
    assert answer.outcome == "danger"
    assert "nearest active fire ~6 km NNW (62 MW)" in answer.text


def test_all_clear_when_nothing_is_near_and_air_is_fine():
    answer = ask("@b Yosemite this weekend?", fc=forecast(aqi=52, fire=(120, 80)))
    assert answer.outcome == "all_clear"
    assert "worst forecast AQI 52 (Moderate), no active fire within 50 km" in answer.text
    assert "Not a guarantee or health advisory" in answer.text


def test_small_hotspots_do_not_count_as_danger():
    answer = ask("@b Yosemite tomorrow?", fc=forecast(aqi=40, fire=(3, 1.2)))
    assert answer.outcome == "all_clear"


def test_all_clear_can_be_turned_off():
    answer = ask("@b Yosemite tomorrow?", cfg=Settings(reply_all_clear=False))
    assert answer.text is None and answer.outcome == "skipped-all-clear"
    danger = ask(
        "@b Yosemite tomorrow?", fc=forecast(fire=(5, 60)), cfg=Settings(reply_all_clear=False)
    )
    assert danger.text is not None


def test_unknown_place_asks_for_a_place_name():
    answer = ask("@b hello", place=None)
    assert answer.outcome == "asked-for-place"
    assert "Which place?" in answer.text and len(answer.text) < MAX_CHARS


def test_trip_beyond_48_hours_says_so_and_still_checks_fires():
    near = ask("@b Yosemite on Dec 20", fc=forecast(fire=(10, 50)))
    assert near.decision.beyond_forecast and near.outcome == "danger"
    assert "48 h" in near.text
    far = ask("@b Yosemite on Dec 20", fc=forecast())
    assert far.outcome == "all_clear" and "only reaches 48 h" in far.text
    assert window_horizons(100, 130) == []
    assert window_horizons(40, 60)[-1] == 48


def test_stable_air_note_is_mentioned_in_the_reply():
    answer = ask("@b Yosemite tomorrow", fc=forecast(fire=(5.9, 62.3), notes=[STABLE]))
    assert "Stable air: smoke may be underestimated." in answer.text


def test_never_all_clear_when_the_fire_feed_did_not_answer():
    with pytest.raises(RuntimeError, match="fire detections unavailable"):
        ask("@b Yosemite tomorrow", fc=forecast(fire_feed=False))
    answer = ask("@b Yosemite tomorrow", fc=forecast(aqi=150, fire_feed=False))
    assert answer.outcome == "danger"  # AQI alone is still worth saying


def test_every_reply_fits_in_280_characters():
    long_place = Place("A" * 70, 1.0, 1.0, "")
    cases = [
        forecast(aqi=300, fire=(2, 3000), notes=[STABLE], degraded=True),
        forecast(aqi=40),
        forecast(fire=(1.2, 99999), notes=["wind unavailable, fire smoke not added", STABLE]),
    ]
    for fc in cases:
        for mention in ("@b trip to X on Dec 30", "@b X this weekend", "@b X in 2 days"):
            for place in (YOSEMITE, long_place):
                text = ask(mention, fc=fc, place=place).text
                assert text is not None and len(text) <= MAX_CHARS
                assert "http" not in text  # links cost extra to post
    decision = decide(forecast(aqi=150, fire=(5, 60)), 0, 48, Thresholds())
    assert (
        len(compose("Yosemite National Park", "this weekend", decision, Thresholds())) <= MAX_CHARS
    )


# ---------------------------------------------------------------- X client and the run loop


class FakeX:
    """Stands in for api.x.com. Records every request."""

    def __init__(self, posts, referenced=None):
        self.posts, self.referenced = posts, referenced or {}
        self.calls = []
        self.post_status = 201

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET" and "/mentions" in url:
            since = re.search(r"since_id=(\d+)", url)
            posts = [p for p in self.posts if not since or int(p["id"]) > int(since.group(1))]
            data = {"data": posts, "includes": {"tweets": list(self.referenced.values())}}
            return 200, json.dumps(data), {}
        if method == "POST" and url.endswith("/2/tweets"):
            if self.post_status == 201:
                return 201, json.dumps({"data": {"id": "999"}}), {}
            return self.post_status, "{}", {}
        return 404, "{}", {}

    def posts_made(self):
        return [c for c in self.calls if c[0] == "POST"]


def mention(post_id, author="42", text="@WildFireBot Yosemite tomorrow?", **extra):
    return {"id": post_id, "author_id": author, "text": text, **extra}


CREDS = Credentials("bearer", "k", "ks", "t", "ts")


def run(fake, state, cfg, fc=None, now=NOW):
    client = XClient(CREDS, send=fake, sleep=lambda _s: None)
    lines: list[str] = []
    result = run_once(
        client,
        state,
        cfg,
        now=lambda: now,
        geocoder=lambda name: YOSEMITE,
        forecaster=lambda args, runtime: fc or forecast(fire=(5, 60)),
        out=lines.append,
    )
    return result, lines


def live(tmp_path, **kw):
    return settings(tmp_path, dry_run=False, bot_user_id="7", **kw)


def test_dry_run_prints_and_never_calls_the_post_endpoint(tmp_path):
    fake = FakeX([mention("100")])
    result, lines = run(fake, State(bot_user_id="7"), settings(tmp_path, bot_user_id="7"))
    assert result.replied == 1
    assert fake.posts_made() == []
    assert lines[0].startswith("[dry run] reply to mention 100 (danger): Heads-up for Yosemite")


def test_live_run_posts_a_threaded_reply_signed_with_oauth1(tmp_path):
    fake = FakeX([mention("100")])
    state = State(bot_user_id="7")
    result, _ = run(fake, state, live(tmp_path))
    assert result.replied == 1
    ((method, url, headers, body),) = fake.posts_made()
    assert url == "https://api.x.com/2/tweets"
    sent = json.loads(body)
    assert sent["reply"] == {"in_reply_to_tweet_id": "100"}
    assert len(sent["text"]) <= MAX_CHARS
    assert (
        headers["Authorization"].startswith("OAuth ")
        and "oauth_signature=" in headers["Authorization"]
    )
    mentions_call = fake.calls[0]
    assert mentions_call[2]["Authorization"] == "Bearer bearer"
    assert "expansions=referenced_tweets.id" in mentions_call[1]
    assert "author_id" in mentions_call[1] and "user.fields" not in mentions_call[1]


def test_parent_post_text_is_used_when_the_mention_is_a_reply(tmp_path):
    parent = {"id": "50", "text": "excited for my trip to yosemite in 2 days"}
    reply = mention(
        "100",
        text="@WildFireBot is this ok?",
        referenced_tweets=[{"type": "replied_to", "id": "50"}],
    )
    seen = {}

    def forecaster(args, runtime):
        seen.update(args)
        return forecast(fire=(5, 60))

    client = XClient(CREDS, send=FakeX([reply], {"50": parent}), sleep=lambda _s: None)
    lines: list[str] = []
    run_once(
        client,
        State(bot_user_id="7"),
        settings(tmp_path, bot_user_id="7"),
        now=lambda: NOW,
        geocoder=lambda name: YOSEMITE if name.lower() == "yosemite" else None,
        forecaster=forecaster,
        out=lines.append,
    )
    assert "in 2 days" in lines[0]
    assert seen["lat"] == YOSEMITE.latitude and max(seen["horizons"]) >= 36


def test_never_replies_twice_to_the_same_mention_and_state_survives_a_restart(tmp_path):
    cfg = live(tmp_path)
    fake = FakeX([mention("100"), mention("101", author="43")])
    state = State(bot_user_id="7")
    first, _ = run(fake, state, cfg)
    assert first.replied == 2 and state.since_id == "101"
    assert load_state(cfg.state_path).handled["100"] == "replied"
    # a restart that lost since_id but kept the file must still not answer again
    reloaded = load_state(cfg.state_path)
    reloaded.since_id = None
    second, _ = run(fake, reloaded, cfg)
    assert second.replied == 0
    assert len(fake.posts_made()) == 2
    # since_id is sent so X only returns newer mentions
    third_state = load_state(cfg.state_path)
    run(fake, third_state, cfg)
    assert "since_id=101" in fake.calls[-1][1]


def test_ignores_own_posts_and_retweets(tmp_path):
    posts = [
        mention("100", author="7"),
        mention("101", referenced_tweets=[{"type": "retweeted", "id": "5"}]),
    ]
    fake = FakeX(posts)
    state = State(bot_user_id="7")
    result, _ = run(fake, state, live(tmp_path))
    assert result.replied == 0 and result.skipped == 2
    assert fake.posts_made() == []


def test_author_daily_limit(tmp_path):
    cfg = live(tmp_path, max_per_author_day=2)
    fake = FakeX([mention(str(100 + i)) for i in range(4)] + [mention("200", author="43")])
    state = State(bot_user_id="7")
    result, _ = run(fake, state, cfg)
    assert result.replied == 3  # two for author 42, one for author 43
    assert state.handled["102"] == "skipped-author-limit"
    # the next day the same author can ask again
    tomorrow = NOW + timedelta(days=1, minutes=1)
    fake2 = FakeX([mention("300")])
    again, _ = run(fake2, state, cfg, now=tomorrow)
    assert again.replied == 1


def test_hourly_limit_leaves_the_rest_for_the_next_poll(tmp_path):
    cfg = live(tmp_path, max_per_hour=2)
    fake = FakeX([mention(str(100 + i), author=str(i)) for i in range(4)])
    state = State(bot_user_id="7")
    result, _ = run(fake, state, cfg)
    assert result.replied == 2 and "hourly" in result.stopped
    assert state.since_id == "101"
    later, _ = run(fake, state, cfg, now=NOW + timedelta(hours=1, minutes=1))
    assert later.replied == 2


def test_a_refused_post_is_not_counted_and_not_retried_forever(tmp_path):
    fake = FakeX([mention("100")])
    fake.post_status = 403
    state = State(bot_user_id="7")
    result, lines = run(fake, state, live(tmp_path))
    assert result.replied == 0 and "post failed" in lines[-1]
    assert state.posts == [] and state.handled["100"].startswith("skipped-post-failed")


def test_forecast_failures_retry_then_give_up_without_replying(tmp_path):
    fake = FakeX([mention("100")])
    state = State(bot_user_id="7")
    cfg = live(tmp_path)
    client = XClient(CREDS, send=fake, sleep=lambda _s: None)

    def broken(args, runtime):
        raise OSError("down")

    for attempt in (1, 2):
        out = run_once(
            client,
            state,
            cfg,
            now=lambda: NOW,
            geocoder=lambda n: YOSEMITE,
            forecaster=broken,
            out=lambda _l: None,
        )
        assert out.stopped and not state.is_handled("100"), attempt
    run_once(
        client,
        state,
        cfg,
        now=lambda: NOW,
        geocoder=lambda n: YOSEMITE,
        forecaster=broken,
        out=lambda _l: None,
    )
    assert state.handled["100"] == "skipped-error"
    assert fake.posts_made() == []


def test_429_honours_the_retry_cap():
    sleeps = []
    calls = []

    def send(method, url, headers, body):
        calls.append(url)
        return 429, "{}", {"retry-after": "5"}

    client = XClient(CREDS, send=send, sleep=sleeps.append)
    with pytest.raises(XRateLimited):
        client.mentions("7", None)
    assert sleeps == [5.0, 5.0] and len(calls) == 3

    def too_long(method, url, headers, body):
        calls.append(url)
        return 429, "{}", {"retry-after": "900"}

    calls.clear()
    with pytest.raises(XRateLimited):
        XClient(CREDS, send=too_long, sleep=sleeps.append).mentions("7", None)
    assert len(calls) == 1  # a quota that long is not slept through


def test_a_5xx_on_post_is_never_retried():
    calls = []

    def send(method, url, headers, body):
        calls.append(method)
        return 503, "{}", {}

    with pytest.raises(XError):
        XClient(CREDS, send=send, sleep=lambda _s: None).reply("hi", "1")
    assert calls == ["POST"]


def test_posting_without_user_credentials_is_refused():
    client = XClient(Credentials(bearer="b"), send=lambda *a: (200, "{}", {}))
    with pytest.raises(XError, match="posting needs"):
        client.reply("hi", "1")


def test_oauth1_signature_matches_the_documented_example():
    # the public example from X's "Creating a signature" page (not real credentials)
    header = oauth1_header(
        "POST",
        "https://api.twitter.com/1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        extra_params={
            "include_entities": "true",
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        },
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp="1318622958",
    )
    signature = unquote(re.search(r'oauth_signature="([^"]+)"', header).group(1))
    assert signature == "tnnArxj06cWHq44gCs1OSKk/jLY="


def test_state_file_round_trip_and_settings_defaults(tmp_path):
    path = tmp_path / "s.json"
    state = State(since_id="5", bot_user_id="7")
    state.mark("5", "replied")
    state.record_post("42", NOW)
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.since_id == "5" and loaded.handled == {"5": "replied"}
    assert loaded.author_posts_last_day("42", NOW) == 1
    assert load_state(tmp_path / "missing.json").since_id is None
    defaults = Settings.from_env({})
    assert defaults.dry_run is True and defaults.reply_all_clear is True
    assert defaults.state_path.name.endswith(".dryrun")
    assert defaults.thresholds == Thresholds(101, 50.0, 10.0)
    live_cfg = Settings.from_env({"WFB_X_DRY_RUN": "0", "WFB_X_REPLY_ALL_CLEAR": "false"})
    assert not live_cfg.dry_run and not live_cfg.reply_all_clear
    assert not live_cfg.state_path.name.endswith(".dryrun")
