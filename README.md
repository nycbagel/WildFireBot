# WildFireBot

WildFireBot is a small, runnable smoke outlook for one location. It looks up
active fires, the wind, and a PM2.5 baseline, then estimates when smoke could
arrive and what that does to the AQI. It is a screening model for a demo. It
is not a regulatory forecast and it is not a medical advice tool.

The calculation needs no training data, so it can run the day a fire starts.
A learned model (LightGBM or Chronos-2) belongs later, once there is a history
of smoke at the location to learn from.

```
FIRMS fires ──┐
NWS wind   ───┼──► downwind?  arrival?  Gaussian PM2.5 ──► + baseline ──► AQI
AirNow     ───┤
Open-Meteo ───┘
```

## Setup

Python 3.11 or newer. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`. Leave a key blank if you do not have it. That source is skipped
and the outlook says so.

| Variable | Used for |
| --- | --- |
| `FIRMS_MAP_KEY` | [NASA FIRMS area API](https://firms.modaps.eosdis.nasa.gov/api/area/). A free MAP_KEY. |
| `AIRNOW_API_KEY` | [AirNow API](https://docs.airnowapi.org/). A free API key. |
| `NWS_USER_AGENT` | `api.weather.gov` requires a User-Agent with an app name and a contact, for example `WildFireBot (you@example.com)`. |

Real environment variables override `.env`. The process never prints the keys.
`.env` is gitignored. This repository is public: do not commit a key, and if
one leaks, revoke it. `.env.example` ships with blank keys on purpose.

Open-Meteo does not need a key.

## Run

```bash
python -m wildfirebot.forecast --lat 37.87 --lon -122.27 --radius-km 150
```

That prints a short readout (nearest fires, whether this location is downwind,
an arrival estimate, and the AQI at 0, 6, 12, 24, and 48 hours) and then a
JSON document after a `--- json ---` line.

A chatbot that only wants the document:

```bash
python -m wildfirebot.forecast --lat 37.87 --lon -122.27 --radius-km 150 --json-only
```

Or, in process:

```python
from wildfirebot import build_forecast

outlook = build_forecast(37.87, -122.27, radius_km=150)
```

Useful knobs:

```bash
python -m wildfirebot.forecast --lat 37.87 --lon -122.27 \
  --source VIIRS_NOAA21_NRT \
  --plume-height-m 500 \
  --emission-factor 0.02 \
  --stability D \
  --downwind-deg 30 \
  --horizons 0,6,12,24,48
```

`--source` accepts `VIIRS_NOAA20_NRT` (the default), `VIIRS_NOAA21_NRT`,
`VIIRS_SNPP_NRT`, and `MODIS_NRT`.

## How the model works

1. **Fires.** FIRMS returns the last day of active-fire pixels in a box around
   the location: `lat`, `lon`, `frp` (MW), `confidence`, `acq_date`, and
   `acq_time`. Pixels outside the great-circle radius are dropped. VIIRS
   confidence is `l` / `n` / `h`. MODIS confidence is 0–100.

2. **Wind.** `api.weather.gov/points/{lat},{lon}` points at the hourly
   forecast. Each horizon uses the wind in effect at that hour.
   `windDirection` is where the wind comes **from**. The plume travels the
   opposite way: a west wind blows toward the east.

3. **Downwind.** For each fire, the great-circle bearing from the fire to the
   location is compared with the wind's direction of travel. The location is
   downwind when that bearing is within ±30° (the edge counts). NWS compass
   points are 22.5° wide, so the sector is a little wider than one point.

4. **Arrival.** `hours = distance / wind speed`. The smoke increment is added
   only once that travel time has elapsed. Horizon 0 is "now", so a fire tens
   of kilometers away does not change the current AQI. The current baseline
   carries smoke that is already overhead. Wind slower than 0.5 m/s is raised
   to 0.5 m/s so the travel time stays finite.

5. **Emission.** `Q (kg/s) = 0.02 kg/MJ × FRP (MW)`. One megawatt is one
   megajoule per second. See the assumptions below for where 0.02 comes from.

6. **Dispersion.** A steady Gaussian plume with rural Briggs (1973)
   Pasquill-Gifford sigmas. The default stability class is D (neutral).
   Ground level includes the image source that reflects the plume off the
   surface:

   ```
   C = Q / (π · u · σy · σz) · exp(-y² / (2 σy²)) · exp(-H² / (2 σz²))
   ```

   `Q` is µg/s, `u` is m/s, the sigmas and `H` are meters, and `C` is µg/m³.
   `H` defaults to 500 m and stands in for plume rise. It is not computed
   from FRP.

   Inside the downwind sector the outlook uses the **centerline** value
   (`y = 0`). The sector is already the wind-direction uncertainty. A class-D
   plume is only about ±10° wide, so applying a second crosswind decay on top
   of the ±30° sector would report about zero for almost every fire. The
   crosswind term is implemented and covered by tests; the outlook does not
   turn it on.

7. **Sum.** Every fire that is downwind and has had time to arrive adds its
   centerline concentration. Adjacent FIRMS pixels are separate sources.

8. **Baseline, then AQI.**
   - At 0 hours, use the AirNow current PM2.5 concentration when the key is
     set and the payload includes one.
   - If AirNow returns only a NowCast AQI, invert it through the same 2024
     breakpoints. That is a stand-in for the missing micrograms. The output
     says when this happened.
   - At 6, 12, 24, and 48 hours, use the Open-Meteo hourly `pm2_5` forecast.
   - If AirNow is missing, Open-Meteo is the baseline at every horizon,
     including 0.
   - If Open-Meteo is missing and AirNow has a current value, that value is
     held forward and labeled as held.
   - `total = baseline + smoke`, then the EPA AQI.

   AirNow's own forecast (from `/aq/forecast/current/`) is reported alongside
   this estimate. It is not added on top.

9. **AQI.** PM2.5 is truncated to 0.1 µg/m³. The index is linear between the
   2024 breakpoints and rounded half up to an integer. Above 325.4 µg/m³ the
   Hazardous slope continues, so the index can pass 500.

   | PM2.5 (µg/m³) | AQI | Category |
   | --- | --- | --- |
   | 0.0 – 9.0 | 0 – 50 | Good |
   | 9.1 – 35.4 | 51 – 100 | Moderate |
   | 35.5 – 55.4 | 101 – 150 | Unhealthy for Sensitive Groups |
   | 55.5 – 125.4 | 151 – 200 | Unhealthy |
   | 125.5 – 225.4 | 201 – 300 | Very Unhealthy |
   | 225.5 – 325.4 | 301 – 500 | Hazardous |

   These are the PM2.5 rows of 40 CFR Part 58 Appendix G after the February
   2024 revision. The 2012 table put the top of Good at 12.0 µg/m³. This code
   uses 9.0. Each category includes a short health message in the EPA sense:
   who is affected, and whether to ease off outdoor activity. It is general
   guidance, not advice for a specific person.

## Assumptions and limits

**Emission factor.** `0.02 kg/MJ` is a knob, not a fit. A coefficient near
0.37 kg of dry fuel per MJ of radiated energy (the scale often traced to
Wooster et al., 2005) times a blended PM2.5 factor of about 50 g per kg of
fuel gives `0.37 × 0.05 ≈ 0.019`, which is rounded to 0.02. Flaming
combustion emits less particulate per kilogram. Smoldering emits more. Real
fires move several-fold around this number. Change it with
`--emission-factor` and say which value you used.

**The fire is treated as if it just started.** Arrival is measured from the
moment you run the tool, not from the satellite overpass. Smoke already on
the way is not reconstructed. The AirNow or Open-Meteo baseline is what
represents air that is already dirty.

**Steady plume, one wind.** After the travel time, the plume is steady. The
wind is the wind at the target at that horizon, not a trajectory along the
path. There is no terrain, no deposition, no chemistry, and no day/night
stability change. Class D is the default. Classes A–F are available, but the
tool does not pick one from cloud cover.

**The sigmas are extrapolated.** Rural Briggs curves were built for roughly
100 m to 10 km. At 50–150 km they only give a scale. The 500 m plume height
is a stand-in for plume rise plus a mixed height, not a calculated value.
Raising it keeps smoke aloft longer. Setting it near 0 treats the fire as a
ground-level source.

**Centerline inside the sector is conservative** relative to a narrow plume
that might miss you, and it is not an ensemble mean. The readout is "if the
plume axis is over this location."

**Pixels are not fires.** A large fire is many VIIRS pixels. Summing them as
independent sources over-counts. The note appears once a search returns 8 or
more detections.

**Calm wind is floored at 0.5 m/s.** Otherwise travel time and concentration
go to infinity. The horizon is marked when that happens.

**Coverage.** FIRMS sees the fires a satellite passed over in the last day,
not every flame. NWS wind stops at the edge of the US forecast grid. AirNow
is US monitoring. Open-Meteo is the global fallback and is a model field, not
a monitor. A missing key or a failed request skips that source and continues.

Do not use the number as an all-clear, a health decision, or a regulatory
comparison.

## Tests

The tests are offline. HTTP is replaced with fixtures and a fake transport.
Nothing in `tests/` calls FIRMS, NWS, AirNow, or Open-Meteo.

```bash
pytest
```

They cover great-circle distance and bearing, the FROM-versus-TOWARD downwind
rule (including the wrap around north), arrival time, the Briggs sigmas, the
closed-form Gaussian (concentration falls with distance and rises with FRP),
the 2024 AQI breakpoints (including 9.0 versus the old 12.0 cutoff), and a
full outlook driven by fixture payloads.

## Layout

```
wildfirebot/
  geo.py            distance, bearing, search box
  plume.py          downwind test, arrival, Gaussian plume
  aqi.py            2024 PM2.5 breakpoints
  clients/          FIRMS, NWS, AirNow, Open-Meteo
  forecast.py       build_forecast() and the CLI
  summary.py        plain-language readout
tests/fixtures/     saved CSV and JSON payloads
```

## Next steps

The Gaussian plume is the cold start. It runs before any smoke history
exists. The useful next step is to **keep the history**: each run's fires,
wind, baseline, and the PM2.5 that was actually observed a few hours later.
That table is the training set. Two models become reasonable once it exists,
and not before:

- **LightGBM** (or another gradient-boosted tree) on a row per location-hour.
  Features can include upwind FRP, distance, wind speed and direction, the
  plume's own increment, stability, and yesterday's PM2.5. The tree's job is
  to correct the plume's bias, which will be large and systematic (pixel
  double-counting, the 0.02 kg/MJ guess, class D everywhere).
- **Chronos-2**, or another time-series model, fine-tuned on the location's
  PM2.5 series, with the plume increment as a covariate. That is a better
  home for the diurnal baseline the Gaussian does not know about. It still
  needs labels. It does not replace the plume on day one.

Physics worth doing before either model, because both will learn the bugs
otherwise: cluster FIRMS pixels into fires, estimate plume rise from FRP, and
use a real mixing height instead of a fixed 500 m.

## Data sources

- NASA FIRMS area API, VIIRS and MODIS near-real-time active fire.
- National Weather Service, `api.weather.gov` hourly forecast.
- AirNow current observations (`/aq/observation/current/ziplatlong/`) and the
  current forecast (`/aq/forecast/current/`). The older
  `/aq/observation/latLong/current/` route returns HTTP 410 and is not called.
- [Open-Meteo air quality](https://open-meteo.com/en/docs/air-quality-api),
  hourly `pm2_5`, no key.
