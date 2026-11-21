# X mention bot

An opt-in bot for X. **It only answers a post that @mentions it.** It never searches for
keywords and never replies to anyone who did not tag it, which keeps it inside X's
automation rules. It is a **dry run by default**: replies are printed, nothing is posted.

## What it does

Someone tags the bot, either in a post or as a reply under a post about a trip:

> *"excited for my trip to Yosemite in 2 days"* ← *"@YourBot is this ok?"*

1. It reads the mention. When the mention is a reply it also reads the post it replies to
   (the X API returns it through `expansions=referenced_tweets.id`).
2. It finds a **place** and an optional **when** with plain rules (`xbot/parse.py`).
   Places come from phrases such as "trip to X", "heading to X", "in X", "near X", or a
   bare capitalised name ("@YourBot Yosemite this weekend?"). Dates understood: "in 2
   days/weeks", today, tonight, tomorrow, this/next weekend, weekday names, "Nov 25", "Nov
   25-27", "25 Nov", "12/3". No date means the next 48 h. Windows are in UTC.
3. It looks the place up with Open-Meteo's keyless geocoder, preferring a park over a
   village of the same name ("Yosemite" is a village in Kentucky and a national park in
   California).
4. It runs the same in-process forecast as the `forecast_smoke_aqi` tool for that spot,
   for the trip window (the model reaches 48 h ahead).
5. It decides:
   - **Danger** if the worst forecast AQI in the window is at least `WFB_X_AQI_THRESHOLD`
     (default 101, Unhealthy for Sensitive Groups) **or** an active fire cluster of at
     least `WFB_X_MIN_FIRE_FRP_MW` (default 10 MW, so tiny hotspots do not count) lies
     within `WFB_X_FIRE_KM` (default 50 km).
   - **All clear** otherwise, as a short answer, unless `WFB_X_REPLY_ALL_CLEAR=false`.
   - **No place found**: it asks for a place name.
   - **Trip more than 48 h away**: it says the forecast only reaches 48 h and reports the
     fires near the place right now.
   - It never says "all clear" if the fire feed did not answer. It retries on the next poll
     and gives up silently after 3 tries.
6. The reply is under 280 characters, has no links (links make a post cost more), names the
   place and window, gives the worst AQI and category or the nearest fire's distance and
   direction, mentions the stable-air note when it applies, and ends with a
   not-a-health-advisory line that points to AirNow and local officials.

Example replies (mocked data, for illustration only):

```
Heads-up for Yosemite National Park (in 2 days): forecast AQI up to 140 (Unhealthy for Sensitive Groups) near +36h; nearest active fire ~31 km NNW (62 MW). Stable air: smoke may be underestimated. Screening estimate, not a health advisory. Check AirNow and local officials.

Yosemite National Park (this weekend): worst forecast AQI 52 (Moderate), no active fire within 50 km. Nearest active fire ~120 km NNW (80 MW). Not a guarantee or health advisory. Check AirNow and local officials.
```

Try the text path without any X account: 

```bash
python -m wildfirebot.xbot --text "@YourBot is this ok?" --parent "trip to Sequoia National Park tomorrow"
```

## Safety rules built in

- Dry run unless `WFB_X_DRY_RUN=0`. A dry run uses its own state file (`.xbot_state.json.dryrun`).
- Never replies twice to one mention: the mention id is saved **before** posting, with
  `since_id`, in a JSON file (`WFB_X_STATE_FILE`, default `.xbot_state.json`).
- Ignores its own posts and retweets.
- At most `WFB_X_MAX_REPLIES_PER_HOUR` replies an hour (default 20; the rest wait for the
  next poll) and `WFB_X_MAX_REPLIES_PER_AUTHOR_PER_DAY` per author (default 3).
- 429 responses wait at most the shared 30 s retry cap, then the run stops and the next
  poll tries again. Failed posts (4xx) are not retried; a 5xx on posting is never retried
  because it could post twice.
- Secrets come only from the environment. Nothing is logged or committed.

## Set-up on X

1. **Create the bot's own X account** (a separate account, not yours). In its settings,
   under *Your account → Automation*, **label it as an automated account** and name the
   managing account (yours). X requires this for bots.
2. **Create a developer app** at the X Developer Console while signed in as a person who
   manages the bot. In the app's *User authentication settings* choose **Read and write**
   and OAuth 1.0a, then **generate the access token and secret while signed in as the bot
   account** (or authorise the app from the bot account). Posting as the wrong account is the
   usual mistake.
3. **Get the keys**: the app's *Bearer token* (reads mentions), *API key and secret*, and the
   bot account's *access token and secret* (posting). Put them in the environment, never in git.
4. **Find the bot's numeric user id** (or just set `X_BOT_USERNAME`; the bot looks it up once and
   remembers it in the state file).
5. **Buy credits.** There is no free tier for new developers.

### API access and cost

X moved to **pay-per-use credits** in 2026 (no Basic or Pro subscription for new
developers). Prices below are what the sources listed on 2026-11-21; X says the Developer
Console shows the binding rates, so check it before relying on them.

| What the bot does | Price |
|---|---|
| Read one of your own mentions (`GET /2/users/{id}/mentions`, owned read) | $0.001 per post returned (up to $0.005 if X does not treat the call as an owned read, e.g. with an app-only bearer token) |
| Create a post (a reply) without a URL | $0.015 per reply |
| Create a post that contains a URL | $0.20 per reply (the bot never adds links) |
| User profile read (author expansion) | $0.010 each, so the bot does not request it |

A busy day of 100 mentions with 100 replies is about $1.60 to $2.00. Reads are billed once
per post per UTC day. Pay-per-use is capped at 3 million post reads a month. You can set a
spending limit in the Developer Console.

Sources: <https://opentweet.io/x-api-pricing> (copied from X's pricing page, checked Oct 2026),
<https://getcheapapi.com/twitter-api-pricing/>, <https://x-preview.mintlify.app/x-api/fundamentals/post-cap>,
and X's own pages at <https://docs.x.com> (the official pricing page blocked automated fetching, so
confirm there).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `X_BEARER_TOKEN` | – | Read mentions |
| `X_BOT_USER_ID` or `X_BOT_USERNAME` | – | Which account's mentions to read |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | – | Post replies (OAuth 1.0a) |
| `WFB_X_DRY_RUN` | `1` | `0` posts for real |
| `WFB_X_REPLY_ALL_CLEAR` | `true` | Answer when there is no danger |
| `WFB_X_AQI_THRESHOLD` | `101` | Forecast AQI that counts as danger |
| `WFB_X_FIRE_KM` | `50` | Fire distance that counts as danger |
| `WFB_X_MIN_FIRE_FRP_MW` | `10` | Smallest fire cluster that counts |
| `WFB_X_MAX_REPLIES_PER_HOUR` | `20` | Hourly post cap |
| `WFB_X_MAX_REPLIES_PER_AUTHOR_PER_DAY` | `3` | Per-author daily cap |
| `WFB_X_INTERVAL_S` | `120` | Seconds between polls in loop mode |
| `WFB_X_STATE_FILE` | `.xbot_state.json` | State file (since_id, replied ids) |
| `FIRMS_MAP_KEY`, `AIRNOW_API_KEY` | – | The forecast's own data keys, as for the rest of WildFireBot |

## Running it

```bash
pip install .
python -m wildfirebot.xbot --once        # poll once and exit (cron, Actions)
python -m wildfirebot.xbot --loop        # poll forever (default), every WFB_X_INTERVAL_S
```

Start in dry run, tag the bot from another account, read the printed replies, and only then
set `WFB_X_DRY_RUN=0`.

- **Render:** create a **Background Worker** from this repo (Docker or Python) with start
  command `python -m wildfirebot.xbot --loop` and a persistent disk for `WFB_X_STATE_FILE`.
  Background workers are not on Render's free plan (check Render's pricing). Without a
  disk the state resets on every deploy, so a restart could answer old mentions again.
- **Cron:** run `python -m wildfirebot.xbot --once` every few minutes with the state file on
  disk.
- **GitHub Actions:** copy `docs/examples/xbot-workflow.yml` to `.github/workflows/xbot.yml` (it
  is kept here because pushing workflow files needs extra token permission). It runs `--once` on
  demand and can be put on a schedule (commented out). Add the secrets `X_BEARER_TOKEN`, `X_API_KEY`, `X_API_SECRET`,
  `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` (plus the data keys) and the variables
  `X_BOT_USER_ID` and `WFB_X_DRY_RUN`. The state is kept with the Actions cache. Scheduled
  runs can be delayed or skipped by GitHub, so this is the least reliable option.

## Limits

It is a screening tool with a rule-based reader. It can misread a place, it uses UTC dates, and
it inherits the forecast's limits (see `docs/methodology.md`), including that the plume model
can read 0 beside a fire in stable night air. It never claims certainty.
