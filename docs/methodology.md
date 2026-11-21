# Methodology

Milestone 1 answers a narrower question than "what will the AQI be?" It asks whether detected fire is upwind of a point over the next hours, and how concentrated that smoke is likely to be if the boundary layer is shallow. The result is a relative score, not a concentration and not an official AQI.

## Feeds

NASA FIRMS area API, `west,south,east,north`, one to five days per call:

`/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/{days}/{date}`

Default order is VIIRS NOAA-21, VIIRS NOAA-20, VIIRS S-NPP, MODIS, then Landsat, all NRT. NRT products already include real-time and ultra-real-time rows. The `version` field carries the processing level (`URT`, `RT`, `NRT`, or standard processing). URT rows are removed upstream once the matching NRT detection arrives, or after about six hours. Landsat NRT covers the US and Canada and has no FRP.

Open-Meteo supplies hourly wind speed and direction at 10 m and at 850 hPa, plus boundary-layer height, in m/s and UTC. `forecast_days` counts calendar days in UTC starting today, so the request runs through the day that holds the last horizon. If Open-Meteo has no wind (for example HTTP 429 when a shared host's address is over its free limit), MET Norway's Locationforecast supplies the 10 m wind instead. It has no 850 hPa wind or boundary-layer height, so hours that need them are flagged. It starts at the current hour, so the hours before it, which the back-trajectory needs, hold the first forecast wind steady, and the output says how many. It is hourly for about 60 hours and six-hourly after that, so the plume AQI asks for wind up to 3 hours past its last horizon. AirNow supplies PM2.5 for the box (`/aq/data/`), with the current observation from `/aq/observation/current/ziplatlong/` as a fallback. The older `/aq/observation/latLong/current/` service was retired on October 1, 2026 and returns 410. The new one sends camelCase fields, a NowCast AQI without a concentration, and no site coordinates, so those rows take the request point and are labeled as a reported AQI. Open-Meteo's air-quality API supplies the hourly background PM2.5 forecast for the plume AQI. OpenAQ v3 supplies PM2.5 (`parameters_id=2`) for the closest locations in the box.

Every client implements `fetch(bbox, time_range) -> SourceResult`.

## Quality checks

Rows are dropped when coordinates are outside -90..90 / -180..180, the timestamp will not parse, confidence is VIIRS/Landsat `l`/`low` or a MODIS number below 30, FRP is negative, non-numeric, or above 100,000 MW, or the MODIS `type` is not a vegetation fire. Blank FRP is kept. Landsat uses that path and does not add to the score.

Hotspots within 3 km are one cluster (single linkage). The centroid is weighted by FRP. A lower-priority satellite does not add a hotspot that sits within 3 km of one already kept, so the same flame is not counted twice. Pixels from one satellite stay in the cluster and their FRP is summed.

## Score

For each forecast hour the air at the target is walked upwind for up to 12 hours. Each step uses that hour's wind at the target. Meteorological direction is the direction the wind comes from, so a north wind steps north. The wind is spatially uniform. That is a milestone-1 limit.

A cluster within 5 km of the target always counts. Any other cluster counts only when the upwind path gets at least 1 km closer to it than the target already is. Closest-approach distance `d` (km) then becomes `exp(-d / 40)`.

Boundary-layer factor is `min(4, 800 / max(height_m, 200))`. A missing height uses 1 and is flagged. Persistence is `exp(-age_hours / 36)` from the cluster's latest detection. FRP is divided by 100 MW.

A 100 MW cluster on the trajectory, under an 800 m boundary layer, detected at that hour, scores about 1. Labels on the score (`little expected smoke`, `light`, `moderate`, `heavy`) are a reading aid. They are not EPA categories.

Wind gaps of up to 3 hours are filled in vector components, so 350° and 10° meet near north. Longer gaps stay missing and those hours have no score. If 10 m wind is still missing and 850 hPa wind exists, transport uses 850 hPa and the hour is marked.

## Plume AQI forecast

`forecast_smoke_aqi` (CLI: `aqi-smoke aqi`) estimates a concentration instead of a relative score. For each horizon (default 0, 6, 12, 24, 48 h) it takes the forecast wind nearest that hour (within 3 h) and sums a steady-state Gaussian plume from every fire cluster:

C = Q / (π u σy σz) · exp(−y² / 2σy²) · exp(−H² / 2σz²)

- Emission Q = 5.5 g/s of PM2.5 per MW of FRP (about 0.368 kg burned per MJ times 15 g PM2.5 per kg). Tunable with `emission_factor`. FRP is clipped at 3000 MW per cluster.
- σy, σz are Pasquill-Gifford (Briggs open-country) widths. Stability class D by default.
- Effective plume height H = 200 m. Distances under 1 km and winds under 1 m/s are clamped.
- A cluster counts only if the target is within 60° of the plume axis and the smoke can arrive in time: distance / wind speed ≤ horizon. At +0 h no new smoke has arrived yet.
- Fire PM2.5 is capped at 500 µg/m³. It is added to the Open-Meteo background (or the nearest monitor's PM2.5, or 8 µg/m³ as a last resort, all labeled), then converted with the EPA breakpoints below.

Gaussian plumes are narrow. A fire 10° off the wind axis at 45 km can add almost nothing, so small wind-direction errors matter a lot. Treat the result as a screening estimate. Once a fire has burned for a day or two and monitors see its smoke, a trained model (milestone 2) should take over.

## Monitors

Where a feed reports PM2.5 in µg/m³, the CLI converts it with the EPA 2024 PM2.5 breakpoints (40 CFR Appendix G to Part 58): truncate to one decimal, apply the piecewise linear index, round half up. The breakpoints are 0.0–9.0, 9.1–35.4, 35.5–55.4, 55.5–125.4, 125.5–225.4, and 225.5–325.4 µg/m³, with AQI 500 at 325.4 and the last segment extended above that. This is not a 24-hour average and not a NowCast. `predict_pm25` raises until milestone 2.

## When a feed fails

HTTP 429 and 5xx, and connection errors, are retried with exponential backoff, waiting at most 30 seconds in total. A 429 whose Retry-After asks for longer, such as a spent hourly or daily quota, is not retried, so the forecast moves on to a cached copy or reports the feed down instead of sleeping. Other 4xx responses are not retried. Usable 200 responses are cached (30 minutes for fires and monitors, 60 for wind). A fresh cache hit does not warn. If the live call fails and an older cache entry parses, it is used and the output says how old it is. FIRMS sources are tried in order; a dead source does not drop the ones that answered. An invalid MAP key stops the FIRMS loop. When Open-Meteo returns no wind, MET Norway is tried for the 10 m wind and the output names the switch; if both fail, the wind is reported missing. MET Norway's 5xx answers are retried like the others, but a 429 is not: after one, MET Norway is not asked again for 10 minutes, or for as long as Retry-After says, and only a cached answer is used. Its answers are kept for 60 minutes even when a refresh is asked for, since its terms say never to repeat a request before it expires. Past about +60 h it gives one step every 6 hours, longer than the 3-hour gap the score fills, so forecast_smoke scores only the hours that land on a step and marks the rest as missing wind; the plume AQI horizons take the nearest wind within 3 hours and are not affected. Missing keys are named in a DEGRADED MODE section. The score is omitted when fires or the arrival wind are unavailable, rather than shown as a confident zero.

## Limits

- One wind column, not a wind field. Terrain, drainage flows, and plume rise are not modeled. 850 hPa is a fallback and a summary, not an injection height.
- FIRMS misses fires under cloud, canopy, and between overpasses. URT can be revised or withdrawn.
- FRP is not an emission factor. Fuel, flaming versus smoldering, and time of day are not in the score.
- The search radius (default 250 km) drops fires beyond it that can still matter.
- The exponential decay is a stand-in for "is this fire still burning," not a fire-growth model.
- Doubling FRP doubles the score. Real smoke chemistry is not that linear.
- Applying 24-hour AQI breakpoints to one monitor hour is only a way to read the current concentration.

Details of the later model and the MCP server are in `docs/roadmap.md`.
