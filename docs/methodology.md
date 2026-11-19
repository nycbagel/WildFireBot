# Methodology

Milestone 1 answers a narrower question than "what will the AQI be?" It asks whether detected fire is upwind of a point over the next hours, and how concentrated that smoke is likely to be if the boundary layer is shallow. The result is a relative score, not a concentration and not an official AQI.

## Feeds

NASA FIRMS area API, `west,south,east,north`, one to five days per call:

`/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/{days}/{date}`

Default order is VIIRS NOAA-21, VIIRS NOAA-20, VIIRS S-NPP, MODIS, then Landsat, all NRT. NRT products already include real-time and ultra-real-time rows. The `version` field carries the processing level (`URT`, `RT`, `NRT`, or standard processing). URT rows are removed upstream once the matching NRT detection arrives, or after about six hours. Landsat NRT covers the US and Canada and has no FRP.

Open-Meteo supplies hourly wind speed and direction at 10 m and at 850 hPa, plus boundary-layer height, in m/s and UTC. AirNow supplies PM2.5 for the box, with the current lat/lon observation as a fallback. OpenAQ v3 supplies PM2.5 (`parameters_id=2`) for the closest locations in the box.

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

## Monitors

Where a feed reports PM2.5 in µg/m³, the CLI converts it with the EPA 2024 PM2.5 breakpoints (40 CFR Appendix G to Part 58): truncate to one decimal, apply the piecewise linear index, round half up. The breakpoints are 0.0–9.0, 9.1–35.4, 35.5–55.4, 55.5–125.4, 125.5–225.4, and 225.5–325.4 µg/m³, with AQI 500 at 325.4 and the last segment extended above that. This is not a 24-hour average and not a NowCast. `predict_pm25` raises until milestone 2.

## When a feed fails

HTTP 429 and 5xx, and connection errors, are retried with exponential backoff. Other 4xx responses are not retried. Usable 200 responses are cached (30 minutes for fires and monitors, 60 for wind). A fresh cache hit does not warn. If the live call fails and an older cache entry parses, it is used and the output says how old it is. FIRMS sources are tried in order; a dead source does not drop the ones that answered. An invalid MAP key stops the FIRMS loop. Missing keys are named in a DEGRADED MODE section. The score is omitted when fires or the arrival wind are unavailable, rather than shown as a confident zero.

## Limits

- One wind column, not a wind field. Terrain, drainage flows, and plume rise are not modeled. 850 hPa is a fallback and a summary, not an injection height.
- FIRMS misses fires under cloud, canopy, and between overpasses. URT can be revised or withdrawn.
- FRP is not an emission factor. Fuel, flaming versus smoldering, and time of day are not in the score.
- The search radius (default 250 km) drops fires beyond it that can still matter.
- The exponential decay is a stand-in for "is this fire still burning," not a fire-growth model.
- Doubling FRP doubles the score. Real smoke chemistry is not that linear.
- Applying 24-hour AQI breakpoints to one monitor hour is only a way to read the current concentration.

Details of the later model and the MCP server are in `docs/roadmap.md`.
