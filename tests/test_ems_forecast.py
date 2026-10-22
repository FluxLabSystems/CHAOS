"""Clear-sky PV forecast (SDD 30.5, 30.6): geometry, irradiance and the forecaster.

The solar-position checks use positions anyone can verify by hand: at solar
noon the sun's elevation is ``90 - |latitude - declination|`` and its azimuth
is due south in the northern hemisphere, due north in the southern. On the
solstices the declination is +/-23.44 degrees and on the equinox it is zero.

Nothing here reads a clock; every instant is passed in.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_ems_state_machine import T0, make_inputs

from chaos.ems.config import EmsConfig
from chaos.ems.derived import NaivePersistenceForecast, compute_derived
from chaos.ems.forecast import (
    ArrayGeometry,
    ClearSkyForecast,
    SiteGeometry,
    SolarPosition,
    array_geometry,
    clear_sky_irradiance,
    plane_of_array_w_m2,
    registry_forecast,
    site_geometry,
    solar_position,
)
from chaos.models.registry import Asset, Location

#: A surveyed site: 45 N on the Greenwich meridian, so solar noon is near 12:00 UTC.
SITE = SiteGeometry(latitude_deg=45.0, longitude_deg=0.0, elevation_m=100.0, timezone="UTC", source="test")
ARRAY = ArrayGeometry(rated_kw=45.0, rating_status="test", tilt_deg=30.0, azimuth_deg=180.0, source="test")
JUNE_SOLSTICE = dt.date(2026, 6, 21)
DECEMBER_SOLSTICE = dt.date(2026, 12, 21)
MARCH_EQUINOX = dt.date(2026, 3, 20)
DECLINATION_MAX = 23.44


def solar_noon(date: dt.date, latitude: float, longitude: float) -> tuple[dt.datetime, SolarPosition]:
    """The minute of the day with the highest sun, by exhaustive scan."""
    midnight = dt.datetime(date.year, date.month, date.day, tzinfo=dt.UTC)
    best: tuple[dt.datetime, SolarPosition] | None = None
    for minute in range(1440):
        when = midnight + dt.timedelta(minutes=minute)
        position = solar_position(when, latitude, longitude)
        if best is None or position.elevation_deg > best[1].elevation_deg:
            best = (when, position)
    assert best is not None
    return best


# ---------------------------------------------------------------------------
# Solar position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("date", "latitude", "expected_elevation", "expected_azimuth"),
    [
        (JUNE_SOLSTICE, 45.0, 90.0 - (45.0 - DECLINATION_MAX), 180.0),
        (DECEMBER_SOLSTICE, 45.0, 90.0 - (45.0 + DECLINATION_MAX), 180.0),
        (MARCH_EQUINOX, 45.0, 45.0, 180.0),
        (DECEMBER_SOLSTICE, -35.0, 90.0 - (35.0 - DECLINATION_MAX), 0.0),
    ],
)
def test_solar_noon_elevation_and_azimuth(date, latitude, expected_elevation, expected_azimuth):
    _, noon = solar_noon(date, latitude, 0.0)
    assert noon.elevation_deg == pytest.approx(expected_elevation, abs=0.3)
    # Angular distance, so that 359.9 and 0.1 both count as due north.
    assert abs((noon.azimuth_deg - expected_azimuth + 180.0) % 360.0 - 180.0) < 0.6


def test_solstice_declinations_and_equation_of_time():
    june = solar_position(dt.datetime(2026, 6, 21, 12, tzinfo=dt.UTC), 45.0, 0.0)
    december = solar_position(dt.datetime(2026, 12, 21, 12, tzinfo=dt.UTC), 45.0, 0.0)
    equinox = solar_position(dt.datetime(2026, 3, 20, 15, tzinfo=dt.UTC), 45.0, 0.0)
    assert june.declination_deg == pytest.approx(DECLINATION_MAX, abs=0.05)
    assert december.declination_deg == pytest.approx(-DECLINATION_MAX, abs=0.05)
    assert equinox.declination_deg == pytest.approx(0.0, abs=0.1)
    # Equation of time: about -1.7 min in late June, +2 min in late December.
    assert june.equation_of_time_min == pytest.approx(-1.7, abs=0.5)
    assert december.equation_of_time_min == pytest.approx(2.0, abs=0.5)


def test_solar_noon_follows_longitude():
    """At 75 W solar noon falls five hours later in UTC than on the meridian."""
    at_meridian, _ = solar_noon(JUNE_SOLSTICE, 45.0, 0.0)
    at_toronto, _ = solar_noon(JUNE_SOLSTICE, 45.0, -75.0)
    assert (at_toronto - at_meridian).total_seconds() == pytest.approx(5 * 3600, abs=120)


def test_sun_is_below_the_horizon_at_midnight():
    position = solar_position(dt.datetime(2026, 6, 21, 0, 0, tzinfo=dt.UTC), 45.0, 0.0)
    assert not position.above_horizon
    assert position.zenith_deg > 90.0


def test_naive_datetimes_are_taken_as_utc():
    aware = solar_position(dt.datetime(2026, 6, 21, 12, tzinfo=dt.UTC), 45.0, 0.0)
    # The naive value is the point of the test: it must be read as UTC.
    naive = solar_position(dt.datetime(2026, 6, 21, 12), 45.0, 0.0)
    assert naive == aware


# ---------------------------------------------------------------------------
# Clear-sky irradiance and transposition
# ---------------------------------------------------------------------------


def test_clear_sky_with_the_sun_overhead_is_near_the_textbook_value():
    overhead = SolarPosition(zenith_deg=0.0, azimuth_deg=180.0, declination_deg=0.0, equation_of_time_min=0.0)
    irradiance = clear_sky_irradiance(overhead, day_of_year=172)
    # 1353 W/m^2 x 0.7 at air mass 1, less the June earth-sun distance.
    assert irradiance.dni == pytest.approx(1353 * 0.7 * (1 - 0.033), rel=0.02)
    assert irradiance.dhi == pytest.approx(0.1 * irradiance.dni, rel=1e-6)
    assert irradiance.ghi == pytest.approx(1.1 * irradiance.dni, rel=1e-6)


def test_clear_sky_is_zero_below_the_horizon():
    night = SolarPosition(zenith_deg=100.0, azimuth_deg=0.0, declination_deg=0.0, equation_of_time_min=0.0)
    irradiance = clear_sky_irradiance(night, day_of_year=1)
    assert (irradiance.dni, irradiance.dhi, irradiance.ghi) == (0.0, 0.0, 0.0)
    assert plane_of_array_w_m2(night, irradiance, tilt_deg=30.0, azimuth_deg=180.0) == 0.0


def test_altitude_increases_direct_irradiance():
    low_sun = SolarPosition(zenith_deg=60.0, azimuth_deg=180.0, declination_deg=0.0, equation_of_time_min=0.0)
    sea_level = clear_sky_irradiance(low_sun, day_of_year=100, elevation_m=0.0)
    mountain = clear_sky_irradiance(low_sun, day_of_year=100, elevation_m=2000.0)
    assert mountain.dni > sea_level.dni


def test_a_tilt_facing_a_low_sun_collects_more_than_a_flat_plane():
    low_sun = SolarPosition(zenith_deg=70.0, azimuth_deg=180.0, declination_deg=0.0, equation_of_time_min=0.0)
    irradiance = clear_sky_irradiance(low_sun, day_of_year=355)
    flat = plane_of_array_w_m2(low_sun, irradiance, tilt_deg=0.0, azimuth_deg=180.0)
    tilted = plane_of_array_w_m2(low_sun, irradiance, tilt_deg=30.0, azimuth_deg=180.0)
    facing_away = plane_of_array_w_m2(low_sun, irradiance, tilt_deg=30.0, azimuth_deg=0.0)
    assert tilted > flat > facing_away


# ---------------------------------------------------------------------------
# The forecaster
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def forecaster() -> ClearSkyForecast:
    return ClearSkyForecast(SITE, ARRAY)


def noon_inputs(forecaster: ClearSkyForecast, *, clearness: float, **overrides):
    """Inputs at solar noon on the June solstice with the array at ``clearness`` x clear sky."""
    noon, _ = solar_noon(JUNE_SOLSTICE, SITE.latitude_deg, SITE.longitude_deg)
    clear_now = forecaster.clear_sky_kw(noon)
    values = {"pv_power_kw": clearness * clear_now, "pv_energy_today_kwh": None}
    values.update(overrides)
    return noon, clear_now, make_inputs(noon, **values)


def test_clear_sky_power_peaks_at_noon_within_the_rating(forecaster):
    noon, position = solar_noon(JUNE_SOLSTICE, SITE.latitude_deg, SITE.longitude_deg)
    peak = forecaster.clear_sky_kw(noon)
    assert 0.6 * ARRAY.rated_kw < peak <= ARRAY.rated_kw * 1.1
    assert forecaster.clear_sky_kw(noon - dt.timedelta(hours=3)) < peak
    assert forecaster.clear_sky_kw(noon + dt.timedelta(hours=12)) == 0.0


def test_clear_sky_day_energy_is_larger_in_june_than_december(forecaster):
    june = forecaster.clear_sky_energy_kwh(dt.datetime(2026, 6, 21, tzinfo=dt.UTC), 24.0)
    december = forecaster.clear_sky_energy_kwh(dt.datetime(2026, 12, 21, tzinfo=dt.UTC), 24.0)
    assert june > 2 * december > 0


def test_present_clearness_persists_over_the_short_horizon(forecaster, config):
    noon, clear_now, inputs = noon_inputs(forecaster, clearness=0.5)
    result = forecaster.forecast(config.forecast_short_horizon_h, now=noon, inputs=inputs, config=config)
    assert result.valid
    assert result.method == "clear_sky_persistence"
    expected = 0.5 * config.forecast_persistence_derate * forecaster.clear_sky_energy_kwh(noon, 6.0)
    assert result.kwh == pytest.approx(expected, rel=1e-6)
    joined = " ".join(result.assumptions)
    assert "present clearness index 0.50" in joined
    assert "no weather feed" in joined
    assert "placeholder" in joined
    assert "pv_power_kw" in result.inputs


def test_measured_power_above_clear_sky_is_capped_at_clear_sky(forecaster, config):
    noon, _, inputs = noon_inputs(forecaster, clearness=1.3)
    result = forecaster.forecast(config.forecast_short_horizon_h, now=noon, inputs=inputs, config=config)
    assert result.kwh == pytest.approx(
        config.forecast_persistence_derate * forecaster.clear_sky_energy_kwh(noon, 6.0), rel=1e-6
    )
    assert "clearness index 1.00" in " ".join(result.assumptions)


def test_day_horizon_prefers_todays_measured_energy(forecaster, config):
    noon, clear_now, _ = noon_inputs(forecaster, clearness=0.9)
    midnight = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    clear_so_far = forecaster.clear_sky_energy_kwh(midnight, (noon - midnight).total_seconds() / 3600)
    inputs = make_inputs(noon, pv_power_kw=0.9 * clear_now, pv_energy_today_kwh=0.3 * clear_so_far)
    result = forecaster.forecast(config.forecast_day_horizon_h, now=noon, inputs=inputs, config=config)
    expected = 0.3 * config.forecast_persistence_derate * forecaster.clear_sky_energy_kwh(noon, 24.0)
    assert result.kwh == pytest.approx(expected, rel=1e-6)
    assert "today's clearness index 0.30" in " ".join(result.assumptions)
    assert "pv_energy_today_kwh" in result.inputs


def test_night_forecast_still_sees_tomorrows_sun(forecaster, config):
    night = dt.datetime(2026, 6, 21, 23, 30, tzinfo=dt.UTC)
    inputs = make_inputs(night, pv_power_kw=0.0, pv_energy_today_kwh=None)
    result = forecaster.forecast(config.forecast_day_horizon_h, now=night, inputs=inputs, config=config)
    assert result.valid
    assert result.kwh > 0.0
    assert "clearness not observable" in " ".join(result.assumptions)
    # The persistence placeholder would have said zero all night.
    naive = NaivePersistenceForecast().forecast(
        config.forecast_day_horizon_h, now=night, inputs=inputs, config=config
    )
    assert naive.kwh == 0.0


def test_invalid_pv_power_produces_no_forecast(forecaster, config):
    inputs = make_inputs(T0, invalid=("pv_power_kw",))
    result = forecaster.forecast(6.0, now=T0, inputs=inputs, config=config)
    assert not result.valid
    assert result.kwh is None


def test_unlocated_site_falls_back_to_persistence_and_says_why(config):
    unlocated = ClearSkyForecast(
        SiteGeometry(
            timezone="America/Toronto", source="registry asset site", open_fields=("property_coordinates",)
        ),
        ARRAY,
    )
    assert not unlocated.available
    inputs = make_inputs(T0, pv_power_kw=10.0)
    result = unlocated.forecast(config.forecast_short_horizon_h, now=T0, inputs=inputs, config=config)
    naive = NaivePersistenceForecast().forecast(
        config.forecast_short_horizon_h, now=T0, inputs=inputs, config=config
    )
    assert result.method == "naive_persistence"
    assert result.kwh == naive.kwh
    assert result.assumptions[0].startswith(
        "site coordinates are an open field in the register (property_coordinates)"
    )
    assert set(naive.assumptions) <= set(result.assumptions)
    assert unlocated.clear_sky_kw(T0) == 0.0


def test_unrated_array_is_reported_not_guessed(config):
    unrated = ClearSkyForecast(SITE, ArrayGeometry(source="registry asset pv"))
    reasons = unrated.unavailable_reasons()
    assert len(reasons) == 1
    assert "rating unknown" in reasons[0]


def test_missing_tilt_and_orientation_are_assumed_and_declared(config):
    bare = ClearSkyForecast(SITE, ArrayGeometry(rated_kw=10.0, source="test"))
    noon, _, inputs = noon_inputs(bare, clearness=1.0)
    result = bare.forecast(6.0, now=noon, inputs=inputs, config=config)
    joined = " ".join(result.assumptions)
    assert "horizontal assumed" in joined
    assert "equator-facing assumed" in joined


def test_compute_derived_accepts_the_clear_sky_forecaster(forecaster, config):
    noon, _, inputs = noon_inputs(forecaster, clearness=0.8)
    derived = compute_derived(inputs, config, now=noon, forecast=forecaster)
    short = derived.get("forecast_pv_next_6h_kwh")
    assert short is not None and short.valid and short.kind == "forecast"
    assert "clear_sky_persistence" in short.basis
    assert derived.value("forecast_pv_next_24h_kwh") > derived.value("forecast_pv_next_6h_kwh") > 0


# ---------------------------------------------------------------------------
# Geometry from the registry
# ---------------------------------------------------------------------------


def test_registry_geometry_is_read_not_invented(db_session):
    db_session.add(
        Asset(
            asset_id="site.site.primary.01",
            domain="site",
            asset_class="site",
            name="site",
            status="concept",
            criticality="critical",
            control_authority="supervisory",
            properties={"timezone": "America/Toronto"},
            open_fields=["property_coordinates"],
        )
    )
    db_session.add(
        Asset(
            asset_id="energy.pv_array.agrivoltaic_field.01",
            domain="energy",
            asset_class="pv_array",
            name="array",
            status="planned",
            criticality="critical",
            control_authority="vendor_native",
            properties={
                "rated_kwdc": 45.0,
                "rated_kwdc_status": "revised_design_target",
                "tilt_deg": 30,
                "orientation": "true_south",
            },
        )
    )
    db_session.flush()

    # The shipped register: timezone known, coordinates an open field.
    site = site_geometry(db_session, "site.site.primary.01")
    assert site.timezone == "America/Toronto"
    assert not site.located
    assert "property_coordinates" in site.open_fields
    array = array_geometry(db_session)
    assert array.rated_kw == 45.0
    assert array.rating_status == "revised_design_target"
    assert array.tilt_deg == 30.0
    assert array.azimuth_deg == 180.0
    unavailable = registry_forecast(db_session, site_id="site.site.primary.01")
    assert not unavailable.available
    assert "property_coordinates" in unavailable.unavailable_reasons()[0]

    # Once the survey lands as a locations row the geometry becomes available.
    db_session.add(
        Location(asset_id="site.site.primary.01", latitude=45.0, longitude=-75.0, elevation_m=120.0)
    )
    db_session.flush()
    located = registry_forecast(db_session, site_id="site.site.primary.01")
    assert located.available
    assert located.site.elevation_m == 120.0
    noon, _ = solar_noon(JUNE_SOLSTICE, 45.0, -75.0)
    assert located.clear_sky_kw(noon) > 0.6 * 45.0


def test_missing_registry_rows_are_reported(db_session):
    assert "not loaded" in site_geometry(db_session, "site.site.primary.01").source
    assert "not loaded" in array_geometry(db_session).source


# ---------------------------------------------------------------------------
# Surplus persistence over the SDD 36 window
# ---------------------------------------------------------------------------


def surplus_at(forecaster, config, when):
    inputs = make_inputs(
        when,
        pv_power_kw=forecaster.clear_sky_kw(when),
        critical_load_kw=0.5,
        general_load_kw=0.5,
        battery_charge_limit_kw=0.0,
        pv_energy_today_kwh=None,
    )
    derived = compute_derived(inputs, config, now=when, forecast=forecaster)
    return derived.value("surplus_power_kw"), derived.value("surplus_sustained_kw")


def test_clear_sky_trend_shrinks_the_sustained_surplus_before_dusk(forecaster, config):
    noon, _ = solar_noon(JUNE_SOLSTICE, SITE.latitude_deg, SITE.longitude_deg)
    surplus_noon, sustained_noon = surplus_at(forecaster, config, noon)
    assert surplus_noon > 0
    # Around noon the next half hour looks like the present.
    assert sustained_noon == pytest.approx(surplus_noon, rel=0.03)

    # Five hours after noon the clear-sky curve is falling fast: the next half
    # hour is forecast well below the present, and the sustained surplus with it.
    late = noon + dt.timedelta(hours=5)
    surplus_late, sustained_late = surplus_at(forecaster, config, late)
    assert surplus_late > 0
    assert 0 < sustained_late < 0.9 * surplus_late
