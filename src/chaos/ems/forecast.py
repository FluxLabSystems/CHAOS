"""Clear-sky PV forecast (SDD 30.5 "forecast and planning state", 30.6).

:class:`ClearSkyForecast` replaces the naive persistence placeholder as the
default :class:`~chaos.ems.derived.PvForecast`. It is still not a weather
forecast, and every value it produces says so. What it adds is *geometry*:

* **Solar position** from the NOAA / Meeus algorithm (declination, equation of
  time, hour angle, zenith and azimuth), good to a few hundredths of a degree
  for 1950-2050. No atmospheric refraction correction is applied; the error is
  confined to the minutes around sunrise and sunset and is irrelevant to an
  energy estimate.
* **Clear-sky irradiance** from the Meinel / Laue direct-normal model with the
  Kasten-Young air mass, a 10 % clear-sky diffuse fraction and an isotropic
  plane-of-array transposition with 0.2 ground albedo. These are textbook
  clear-sky relationships, not a site calibration.
* **Persistence of the clearness index.** The measured PV power is compared
  with the clear-sky power at the same instant and that ratio -- how cloudy it
  is *now* -- is assumed to persist over the horizon. For the day horizon the
  ratio of today's measured energy to today's clear-sky energy is preferred.
  When the sun is down and nothing was measured today, clear sky is assumed and
  the configured derate is the only margin, which the result states.

Everything the model needs about the site comes from the registry and nothing
is invented: the site record carries the timezone and, once surveyed, the
coordinates; the PV array record carries the rating, tilt and orientation.
Until the owner supplies coordinates (``property_coordinates`` is an open
field on the site asset today) the clear-sky geometry is *unavailable* and the
forecaster falls back to the documented persistence placeholder, saying why.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from chaos.ems.config import EmsConfig
from chaos.ems.derived import ForecastResult, NaivePersistenceForecast, PvForecast
from chaos.ems.inputs import PV_ARRAY, EmsInputs
from chaos.models.registry import Asset, Location

#: Solar constant used by the Meinel / Laue clear-sky model (W/m^2).
SOLAR_CONSTANT_W_M2 = 1353.0
#: Clear-sky diffuse irradiance as a fraction of the direct-horizontal component.
CLEAR_SKY_DIFFUSE_FRACTION = 0.10
#: Isotropic ground-reflected fraction for the plane-of-array transposition.
GROUND_ALBEDO = 0.20
#: Below this fraction of the array rating the clear-sky power is too small to
#: anchor a clearness index (twilight, or the minutes around sunrise).
CLEARNESS_MIN_CLEAR_FRACTION = 0.05
#: Today's clear-sky energy must reach this fraction of the rating (in kWh per
#: kW) before today's measured energy is trusted as a clearness index.
DAILY_CLEARNESS_MIN_KWH_PER_KW = 0.25
#: Register orientation vocabulary -> azimuth measured clockwise from north.
ORIENTATION_AZIMUTH_DEG: dict[str, float] = {
    "north": 0.0,
    "true_north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "true_east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "true_south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "true_west": 270.0,
    "northwest": 315.0,
}


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SolarPosition:
    """Where the sun is for one instant and place."""

    zenith_deg: float
    #: Clockwise from true north.
    azimuth_deg: float
    declination_deg: float
    equation_of_time_min: float

    @property
    def elevation_deg(self) -> float:
        return 90.0 - self.zenith_deg

    @property
    def above_horizon(self) -> bool:
        return self.zenith_deg < 90.0


def _julian_day(when: dt.datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return 2440587.5 + when.timestamp() / 86400.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def solar_position(when: dt.datetime, latitude_deg: float, longitude_deg: float) -> SolarPosition:
    """NOAA solar position (Meeus, *Astronomical Algorithms*).

    ``longitude_deg`` is positive east. ``when`` is taken as UTC when naive.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    utc = when.astimezone(dt.UTC)
    t = (_julian_day(utc) - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anom)
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = mean_long + centre
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_long = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    obliquity0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    obliquity = math.radians(obliquity0 + 0.00256 * math.cos(omega))
    declination = math.asin(math.sin(obliquity) * math.sin(apparent_long))

    y = math.tan(obliquity / 2.0) ** 2
    l0 = math.radians(mean_long)
    equation_of_time = 4.0 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * eccentricity * math.sin(m)
        + 4 * eccentricity * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m)
    )

    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0 + utc.microsecond / 60e6
    true_solar_time = (minutes + equation_of_time + 4.0 * longitude_deg) % 1440.0
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)

    lat = math.radians(_clamp(latitude_deg, -89.999, 89.999))
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(
        hour_angle
    )
    zenith = math.acos(_clamp(cos_zenith, -1.0, 1.0))

    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-9:
        azimuth = 180.0 if latitude_deg >= 0 else 0.0
    else:
        cos_az = (math.sin(lat) * math.cos(zenith) - math.sin(declination)) / (math.cos(lat) * sin_zenith)
        angle = math.degrees(math.acos(_clamp(cos_az, -1.0, 1.0)))
        azimuth = (angle + 180.0) % 360.0 if hour_angle > 0 else (540.0 - angle) % 360.0

    return SolarPosition(
        zenith_deg=math.degrees(zenith),
        azimuth_deg=azimuth,
        declination_deg=math.degrees(declination),
        equation_of_time_min=equation_of_time,
    )


@dataclass(frozen=True)
class ClearSkyIrradiance:
    """Clear-sky components in W/m^2."""

    dni: float
    dhi: float
    ghi: float


def clear_sky_irradiance(
    position: SolarPosition, *, day_of_year: int, elevation_m: float = 0.0
) -> ClearSkyIrradiance:
    """Meinel / Laue direct-normal clear-sky model with Kasten-Young air mass.

    ``DNI = E0 * ((1 - 0.14 h) * 0.7 ** (AM ** 0.678) + 0.14 h)`` with ``h`` the
    site elevation in km and ``E0`` the solar constant corrected for the
    earth-sun distance. Diffuse is taken as 10 % of the direct-horizontal
    component, the usual clear-sky approximation.
    """
    if not position.above_horizon:
        return ClearSkyIrradiance(0.0, 0.0, 0.0)
    zenith = position.zenith_deg
    cos_zenith = math.cos(math.radians(zenith))
    air_mass = 1.0 / (cos_zenith + 0.50572 * (96.07995 - zenith) ** -1.6364)
    altitude_km = max(elevation_m, 0.0) / 1000.0
    extraterrestrial = SOLAR_CONSTANT_W_M2 * (1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0))
    dni = extraterrestrial * ((1.0 - 0.14 * altitude_km) * 0.7 ** (air_mass**0.678) + 0.14 * altitude_km)
    dhi = CLEAR_SKY_DIFFUSE_FRACTION * dni * cos_zenith
    return ClearSkyIrradiance(dni=dni, dhi=dhi, ghi=dni * cos_zenith + dhi)


def plane_of_array_w_m2(
    position: SolarPosition, irradiance: ClearSkyIrradiance, *, tilt_deg: float, azimuth_deg: float
) -> float:
    """Isotropic-sky transposition onto a fixed tilted plane."""
    if not position.above_horizon:
        return 0.0
    zenith = math.radians(position.zenith_deg)
    tilt = math.radians(tilt_deg)
    cos_incidence = math.cos(zenith) * math.cos(tilt) + math.sin(zenith) * math.sin(tilt) * math.cos(
        math.radians(position.azimuth_deg - azimuth_deg)
    )
    beam = irradiance.dni * max(cos_incidence, 0.0)
    sky = irradiance.dhi * (1.0 + math.cos(tilt)) / 2.0
    ground = irradiance.ghi * GROUND_ALBEDO * (1.0 - math.cos(tilt)) / 2.0
    return beam + sky + ground


# ---------------------------------------------------------------------------
# What the registry knows about the site and the array
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteGeometry:
    """Where the site is. ``None`` means the register does not say."""

    latitude_deg: float | None = None
    #: Positive east.
    longitude_deg: float | None = None
    elevation_m: float | None = None
    timezone: str | None = None
    source: str = "unspecified"
    open_fields: tuple[str, ...] = ()

    @property
    def located(self) -> bool:
        return self.latitude_deg is not None and self.longitude_deg is not None


@dataclass(frozen=True)
class ArrayGeometry:
    """The PV array as the register describes it."""

    rated_kw: float | None = None
    rating_status: str | None = None
    tilt_deg: float | None = None
    #: Clockwise from true north; ``None`` when the register gives no orientation.
    azimuth_deg: float | None = None
    source: str = "unspecified"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def site_geometry(session: Session, site_id: str) -> SiteGeometry:
    """Read the site's coordinates, elevation and timezone from the registry.

    Coordinates come from the ``locations`` row the loader writes only when the
    register actually supplies numbers; the site asset's properties are the
    fallback. Nothing is defaulted.
    """
    asset = session.get(Asset, site_id)
    if asset is None:
        return SiteGeometry(source=f"registry asset {site_id} is not loaded")
    properties = asset.properties or {}
    location = session.get(Location, site_id)
    latitude = _number(location.latitude) if location is not None else None
    longitude = _number(location.longitude) if location is not None else None
    elevation = _number(location.elevation_m) if location is not None else None
    if latitude is None:
        latitude = _number(properties.get("latitude"))
    if longitude is None:
        longitude = _number(properties.get("longitude"))
    if elevation is None:
        elevation = _number(properties.get("elevation_m"))
    timezone = properties.get("timezone")
    return SiteGeometry(
        latitude_deg=latitude,
        longitude_deg=longitude,
        elevation_m=elevation,
        timezone=str(timezone) if isinstance(timezone, str) and timezone else None,
        source=f"registry asset {site_id}",
        open_fields=tuple(asset.open_fields or ()),
    )


def array_geometry(session: Session, asset_id: str = PV_ARRAY) -> ArrayGeometry:
    """Read the array rating, tilt and orientation from the registry."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        return ArrayGeometry(source=f"registry asset {asset_id} is not loaded")
    properties = asset.properties or {}
    rated = _number(properties.get("rated_kwdc"))
    if rated is None:
        rated = _number(properties.get("rated_kw"))
    status = properties.get("rated_kwdc_status") or properties.get("rated_kw_status")
    azimuth = _number(properties.get("azimuth_deg"))
    if azimuth is None:
        orientation = properties.get("orientation")
        if isinstance(orientation, str):
            azimuth = ORIENTATION_AZIMUTH_DEG.get(orientation.strip().lower())
    return ArrayGeometry(
        rated_kw=rated,
        rating_status=str(status) if status else None,
        tilt_deg=_number(properties.get("tilt_deg")),
        azimuth_deg=azimuth,
        source=f"registry asset {asset_id}",
    )


# ---------------------------------------------------------------------------
# The forecaster
# ---------------------------------------------------------------------------


class ClearSkyForecast:
    """Clear-sky geometry scaled by persistence of the present clearness.

    ``kWh = clearness * derate * integral(clear-sky power over the horizon)``

    * *clearness* is measured PV power over clear-sky power now (clamped to
      0..1), or today's measured energy over today's clear-sky energy for the
      day horizon, or 1 when neither is observable;
    * *derate* is ``EmsConfig.forecast_persistence_derate``, kept as the
      forecast's confidence margin;
    * the integral is evaluated in ``step_min`` steps from the array's rating,
      tilt and orientation.

    When the register cannot locate the site or rate the array the forecaster
    delegates to :class:`NaivePersistenceForecast` and prefixes the reason.
    """

    method = "clear_sky_persistence"

    def __init__(
        self,
        site: SiteGeometry,
        array: ArrayGeometry,
        *,
        step_min: float = 10.0,
        fallback: PvForecast | None = None,
    ) -> None:
        self.site = site
        self.array = array
        self.step_min = step_min
        self.fallback = fallback or NaivePersistenceForecast()

    # -- geometry --------------------------------------------------------
    def unavailable_reasons(self) -> list[str]:
        """Why the clear-sky estimate cannot be produced, or an empty list."""
        reasons: list[str] = []
        if not self.site.located:
            if "property_coordinates" in self.site.open_fields:
                reasons.append(
                    "site coordinates are an open field in the register (property_coordinates); "
                    "clear-sky geometry unavailable"
                )
            else:
                reasons.append(
                    f"site coordinates unknown ({self.site.source}); clear-sky geometry unavailable"
                )
        if self.array.rated_kw is None or self.array.rated_kw <= 0:
            reasons.append(f"PV array rating unknown ({self.array.source}); clear-sky estimate unavailable")
        return reasons

    @property
    def available(self) -> bool:
        return not self.unavailable_reasons()

    def _tilt_deg(self) -> float:
        return self.array.tilt_deg if self.array.tilt_deg is not None else 0.0

    def _azimuth_deg(self) -> float:
        if self.array.azimuth_deg is not None:
            return self.array.azimuth_deg
        return 180.0 if (self.site.latitude_deg or 0.0) >= 0 else 0.0

    def clear_sky_kw(self, when: dt.datetime) -> float:
        """Undetated clear-sky DC power of the array at ``when``."""
        if not self.available:
            return 0.0
        assert self.site.latitude_deg is not None and self.site.longitude_deg is not None
        assert self.array.rated_kw is not None
        position = solar_position(when, self.site.latitude_deg, self.site.longitude_deg)
        irradiance = clear_sky_irradiance(
            position,
            day_of_year=when.astimezone(dt.UTC).timetuple().tm_yday
            if when.tzinfo
            else when.timetuple().tm_yday,
            elevation_m=self.site.elevation_m or 0.0,
        )
        poa = plane_of_array_w_m2(
            position, irradiance, tilt_deg=self._tilt_deg(), azimuth_deg=self._azimuth_deg()
        )
        return self.array.rated_kw * poa / 1000.0

    def clear_sky_energy_kwh(self, start: dt.datetime, hours: float) -> float:
        """Undetated clear-sky energy over ``hours`` from ``start`` (midpoint rule)."""
        if hours <= 0 or not self.available:
            return 0.0
        step_h = self.step_min / 60.0
        steps = max(math.ceil(hours / step_h), 1)
        total = 0.0
        elapsed = 0.0
        for _ in range(steps):
            width = min(step_h, hours - elapsed)
            if width <= 0:
                break
            midpoint = start + dt.timedelta(hours=elapsed + width / 2.0)
            total += self.clear_sky_kw(midpoint) * width
            elapsed += width
        return total

    def _local_midnight(self, now: dt.datetime) -> tuple[dt.datetime | None, str]:
        """Local midnight in the site's timezone, or why it is unknown."""
        if not self.site.timezone:
            return None, "site timezone not in the register"
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(self.site.timezone)
        except Exception:  # pragma: no cover - depends on the host tz database
            return None, f"timezone {self.site.timezone!r} is not available on this host"
        local = now.astimezone(zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(dt.UTC), self.site.timezone

    # -- the forecast ----------------------------------------------------
    def forecast(
        self, horizon_h: float, *, now: dt.datetime, inputs: EmsInputs, config: EmsConfig
    ) -> ForecastResult:
        unavailable = self.unavailable_reasons()
        if unavailable:
            result = self.fallback.forecast(horizon_h, now=now, inputs=inputs, config=config)
            return ForecastResult(
                kwh=result.kwh,
                valid=result.valid,
                method=result.method,
                assumptions=tuple(unavailable) + tuple(result.assumptions),
                inputs=result.inputs,
            )

        pv_kw = inputs.numeric("pv_power_kw")
        if pv_kw is None:
            return ForecastResult(
                kwh=None,
                valid=False,
                method=self.method,
                assumptions=("PV power invalid; no forecast produced",),
                inputs=("pv_power_kw",),
            )

        assert self.array.rated_kw is not None
        rated = self.array.rated_kw
        derate = config.forecast_persistence_derate
        clear_now = self.clear_sky_kw(now)
        clear_horizon = self.clear_sky_energy_kwh(now, horizon_h)

        assumptions: list[str] = [
            f"clear-sky model: solar geometry at {self.site.latitude_deg:.3f}, {self.site.longitude_deg:.3f} "
            f"({self.site.source}); array {rated:g} kW"
            + (f" ({self.array.rating_status})" if self.array.rating_status else "")
            + f", tilt {self._tilt_deg():g} deg, azimuth {self._azimuth_deg():g} deg; no weather feed",
            f"derate {derate:g} applied as the forecast margin",
            "placeholder for an irradiance/weather forecast service: cloud, snow and soiling are not modelled",
        ]
        if self.array.tilt_deg is None:
            assumptions.append("array tilt not in the register; horizontal assumed")
        if self.array.azimuth_deg is None:
            assumptions.append("array orientation not in the register; equator-facing assumed")
        used: list[str] = ["pv_power_kw"]

        # Clearness from the present instant (sun up) and from today's energy.
        clearness_now: float | None = None
        if clear_now >= CLEARNESS_MIN_CLEAR_FRACTION * rated:
            clearness_now = _clamp(pv_kw / clear_now, 0.0, 1.0)

        clearness_day: float | None = None
        clearness_day_note = ""
        today_kwh = inputs.numeric("pv_energy_today_kwh")
        if today_kwh is not None:
            midnight, zone_note = self._local_midnight(now)
            if midnight is None:
                clearness_day_note = f"today's PV energy not usable as a clearness index: {zone_note}"
            else:
                clear_today = self.clear_sky_energy_kwh(midnight, (now - midnight).total_seconds() / 3600.0)
                if clear_today >= DAILY_CLEARNESS_MIN_KWH_PER_KW * rated:
                    clearness_day = _clamp(today_kwh / clear_today, 0.0, 1.0)
                    clearness_day_note = (
                        f"today's clearness index {clearness_day:.2f} (measured {today_kwh:.1f} kWh vs "
                        f"clear-sky {clear_today:.1f} kWh since local midnight, {zone_note})"
                    )

        short_horizon = horizon_h <= config.forecast_short_horizon_h
        if short_horizon:
            order = ((clearness_now, "now"), (clearness_day, "day"))
        else:
            order = ((clearness_day, "day"), (clearness_now, "now"))
        clearness = 1.0
        source = "none"
        for value, label in order:
            if value is not None:
                clearness, source = value, label
                break

        if source == "now":
            assumptions.append(
                f"present clearness index {clearness:.2f} (measured {pv_kw:.1f} kW vs clear-sky "
                f"{clear_now:.1f} kW) persisted over the horizon"
            )
            if clearness_day_note:
                assumptions.append(clearness_day_note)
        elif source == "day":
            assumptions.append(clearness_day_note + " persisted over the horizon")
            used.append("pv_energy_today_kwh")
        else:
            assumptions.append(
                "clearness not observable (sun below the horizon, no usable energy today); "
                "clear sky assumed with the derate as the only margin"
            )
            if clearness_day_note:
                assumptions.append(clearness_day_note)

        return ForecastResult(
            kwh=clearness * derate * clear_horizon,
            valid=True,
            method=self.method,
            assumptions=tuple(assumptions),
            inputs=tuple(used),
        )


def registry_forecast(
    session: Session,
    *,
    site_id: str,
    pv_asset_id: str = PV_ARRAY,
    step_min: float = 10.0,
) -> ClearSkyForecast:
    """The forecaster for this registry: site and array geometry read from it."""
    return ClearSkyForecast(
        site_geometry(session, site_id), array_geometry(session, pv_asset_id), step_min=step_min
    )
