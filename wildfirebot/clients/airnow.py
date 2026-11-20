"""AirNow current observations and the current forecast.

The old observation route ``/aq/observation/latLong/current/`` returns HTTP 410.
This client uses the replacement routes:

* ``https://www.airnowapi.org/aq/observation/current/ziplatlong/``
* ``https://www.airnowapi.org/aq/forecast/current/``

Both are queried with ``format=application/json``, latitude, longitude,
``distance=25``, and ``API_KEY``. The new observation payload often has a
NowCast AQI (``nowcastAQI``) and no concentration. When that happens the AQI
is inverted through the 2024 PM2.5 breakpoints. An explicit concentration
field wins when one is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from wildfirebot.aqi import pm25_from_aqi
from wildfirebot.clients.http import ClientError, Transport, get_text, parse_json, redact
from wildfirebot.geo import format_coord

AIRNOW_OBSERVATION_URL = "https://www.airnowapi.org/aq/observation/current/ziplatlong/"
AIRNOW_FORECAST_URL = "https://www.airnowapi.org/aq/forecast/current/"

_CONCENTRATION_KEYS = (
    "concentration",
    "Concentration",
    "rawConcentration",
    "RawConcentration",
    "pm25Concentration",
    "PM25Concentration",
)
_AQI_KEYS = ("nowcastAQI", "NowCastAQI", "nowCastAQI", "aqi", "AQI")


@dataclass(frozen=True)
class AirNowForecast:
    date: str
    parameter: str
    aqi: int | None
    category: str
    reporting_area: str

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "parameter": self.parameter,
            "aqi": self.aqi,
            "category": self.category,
            "reporting_area": self.reporting_area,
        }


@dataclass(frozen=True)
class AirNowResult:
    status: str
    detail: str
    pm25_ug_m3: float | None = None
    pm25_from: str | None = None
    aqi: int | None = None
    category: str | None = None
    reporting_area: str | None = None
    forecasts: tuple[AirNowForecast, ...] = field(default_factory=tuple)


def _is_pm25(name: object) -> bool:
    token = "".join(character for character in str(name).upper() if character.isalnum())
    return token == "PM25" or token.startswith("PM25")


def _first(row: dict[str, object], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _category_name(row: dict[str, object]) -> str:
    flat = _first(row, ("aqiCategoryName", "AqiCategoryName", "categoryName", "CategoryName"))
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    nested = row.get("Category") or row.get("category")
    if isinstance(nested, dict):
        name = nested.get("Name") or nested.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


def _reporting_area(row: dict[str, object]) -> str:
    for key in ("reportingAreaName", "ReportingAreaName", "reportingArea", "ReportingArea"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _message_text(value: object) -> str:
    """Pull a short error string out of the shapes AirNow actually returns."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_message_text(item) for item in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("Message", "message", "WebServiceError"):
            if key in value and value[key] not in (None, ""):
                return _message_text(value[key])
    return ""


def _records(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        if "WebServiceError" in payload or (
            ("Message" in payload or "message" in payload)
            and "parameterName" not in payload
            and "ParameterName" not in payload
        ):
            message = _message_text(payload) or "AirNow rejected the request"
            raise ClientError(message)
        for key in ("data", "Data", "observations", "forecasts"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ClientError("AirNow response was not a list of observations")
    records: list[dict[str, object]] = []
    for item in payload:
        if isinstance(item, dict):
            if ("Message" in item or "message" in item) and "parameterName" not in item and "ParameterName" not in item:
                raise ClientError(_message_text(item) or "AirNow rejected the request")
            records.append(item)
    return records


def _pm25_observation(records: list[dict[str, object]]) -> dict[str, object] | None:
    for record in records:
        name = record.get("parameterName") or record.get("ParameterName") or ""
        if _is_pm25(name):
            return record
    return None


def _forecasts(records: list[dict[str, object]]) -> tuple[AirNowForecast, ...]:
    forecasts: list[AirNowForecast] = []
    for record in records:
        name = str(record.get("parameterName") or record.get("ParameterName") or "").strip()
        if not name:
            continue
        aqi_value = _float_or_none(_first(record, _AQI_KEYS))
        date = str(
            record.get("dateForecast")
            or record.get("DateForecast")
            or record.get("dateObserved")
            or record.get("DateObserved")
            or ""
        ).strip()
        forecasts.append(
            AirNowForecast(
                date=date,
                parameter=name,
                aqi=None if aqi_value is None else int(round(aqi_value)),
                category=_category_name(record),
                reporting_area=_reporting_area(record),
            )
        )
    return tuple(forecasts)


def _query(lat: float, lon: float, api_key: str) -> str:
    # safe="/" keeps format=application/json readable. The key is still encoded.
    return urlencode(
        {
            "format": "application/json",
            "latitude": format_coord(lat),
            "longitude": format_coord(lon),
            "distance": "25",
            "API_KEY": api_key,
        },
        safe="/",
    )


def _http_failure(kind: str, status: int, body: str, api_key: str) -> str:
    detail = f"AirNow {kind} failed (HTTP {status})"
    try:
        message = _message_text(parse_json(body))
    except ClientError:
        message = ""
    if message:
        detail = f"{detail}: {redact(message, [api_key])}"
    return detail


def fetch_airnow(
    lat: float,
    lon: float,
    *,
    api_key: str,
    user_agent: str,
    transport: Transport | None = None,
) -> AirNowResult:
    """Fetch the current PM2.5 observation and the current AirNow forecast.

    A missing key skips both calls. If the observation call fails and the
    forecast call works, the forecast is still returned and the status says
    the observation failed. Concentration is preferred over an inverted AQI.
    """

    if not api_key:
        return AirNowResult(
            status="skipped",
            detail="AIRNOW_API_KEY is not set, so AirNow was skipped.",
        )
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    observation_detail = ""
    observation: dict[str, object] | None = None
    try:
        status, body = get_text(
            f"{AIRNOW_OBSERVATION_URL}?{_query(lat, lon, api_key)}",
            headers=headers,
            transport=transport,
        )
        if status != 200:
            observation_detail = _http_failure("observations", status, body, api_key)
        else:
            observation = _pm25_observation(_records(parse_json(body)))
            if observation is None:
                observation_detail = "AirNow returned no PM2.5 observation for this location."
    except ClientError as exc:
        observation_detail = redact(str(exc), [api_key])
    except Exception as exc:
        observation_detail = f"AirNow observations failed ({exc.__class__.__name__})"

    forecasts: tuple[AirNowForecast, ...] = ()
    forecast_detail = ""
    try:
        status, body = get_text(
            f"{AIRNOW_FORECAST_URL}?{_query(lat, lon, api_key)}",
            headers=headers,
            transport=transport,
        )
        if status != 200:
            forecast_detail = _http_failure("forecast", status, body, api_key)
        else:
            forecasts = _forecasts(_records(parse_json(body)))
    except ClientError as exc:
        forecast_detail = redact(str(exc), [api_key])
    except Exception as exc:
        forecast_detail = f"AirNow forecast failed ({exc.__class__.__name__})"

    pm25: float | None = None
    pm25_from: str | None = None
    aqi: int | None = None
    category: str | None = None
    reporting_area: str | None = None
    if observation is not None:
        reporting_area = _reporting_area(observation) or None
        category = _category_name(observation) or None
        aqi_value = _float_or_none(_first(observation, _AQI_KEYS))
        if aqi_value is not None:
            aqi = int(round(aqi_value))
        concentration = _float_or_none(_first(observation, _CONCENTRATION_KEYS))
        if concentration is not None and concentration >= 0:
            pm25 = concentration
            pm25_from = "concentration"
        elif aqi is not None:
            pm25 = pm25_from_aqi(aqi)
            pm25_from = "inverted_aqi"

    parts = [part for part in (observation_detail, forecast_detail) if part]
    if pm25 is not None and pm25_from == "concentration":
        where = f" for {reporting_area}" if reporting_area else ""
        parts.insert(0, f"AirNow PM2.5{where}: {pm25:.1f} µg/m³.")
    elif pm25 is not None and pm25_from == "inverted_aqi":
        where = f" for {reporting_area}" if reporting_area else ""
        parts.insert(
            0,
            f"AirNow PM2.5{where}: NowCast AQI {aqi} with no concentration, "
            f"inverted to {pm25:.1f} µg/m³.",
        )
    if forecasts and not forecast_detail:
        parts.append(f"AirNow forecast returned {len(forecasts)} pollutant rows.")

    if pm25 is None and not forecasts:
        return AirNowResult(
            status="error",
            detail="; ".join(parts) or "AirNow returned nothing usable.",
            forecasts=forecasts,
        )
    status_name = "ok" if pm25 is not None or forecasts else "error"
    return AirNowResult(
        status=status_name,
        detail="; ".join(parts) or "AirNow current data loaded.",
        pm25_ug_m3=pm25,
        pm25_from=pm25_from,
        aqi=aqi,
        category=category,
        reporting_area=reporting_area,
        forecasts=forecasts,
    )
