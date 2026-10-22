"""Clear-sky irradiance model shared by the EMS forecaster and the simulator.

One model and one set of constants, so the simulated homestead and the energy
manager's forecast of it cannot drift apart. :mod:`chaos.ems.forecast` and
:mod:`simulator.components.solar` each keep their own solar geometry -- the
forecaster the NOAA / Meeus position, the simulator the Cooper / Spencer closed
forms its docstring names -- and both bring the resulting sun zenith here.

The model, with its sources:

* **Extraterrestrial normal irradiance**: the solar constant scaled by the
  earth-sun distance factor ``1 + 0.033 cos(2 pi n / 365)`` for day of year
  ``n`` (Duffie & Beckman, *Solar Engineering of Thermal Processes*,
  eq. 1.4.1a).
* **Relative air mass**: Kasten & Young (1989),
  ``1 / (cos z + 0.50572 (96.07995 - z) ** -1.6364)`` with ``z`` the zenith
  angle in degrees; finite at the horizon.
* **Direct normal irradiance**: the Meinel & Meinel (1976) clear-sky fit
  ``0.7 ** (AM ** 0.678)`` with Laue's (1970) altitude correction,
  ``DNI = E0 * ((1 - 0.14 h) * 0.7 ** (AM ** 0.678) + 0.14 h)`` for ``h`` the
  site elevation in km.
* **Diffuse**: 10 % of the direct-horizontal component, the usual clear-sky
  approximation -- not a measured sky.
* **Plane of array**: the isotropic-sky transposition (Liu & Jordan) with a
  ground-reflected term at a configurable albedo.

The solar constant is **1353 W/m^2**, the value the Meinel fit was published
against (the NASA / ASTM standard of its day). The modern mean total solar
irradiance is 1361 W/m^2 (Kopp & Lean 2011); the 0.6 % difference is far inside
the uncertainty of a clear-sky fit with no site calibration, and keeping the
published pairing avoids presenting a hybrid as either source. Before this
module existed the simulator used 1361 with the Meinel fit and no earth-sun
distance factor while the forecaster used 1353 with both, so the two disagreed
by up to about 4 % for the same sky.

These are textbook relationships, not a site calibration: cloud, snow, soiling
and aerosol are not modelled here, and every consumer says so in its own words.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Solar constant the Meinel / Laue clear-sky fit was published against (W/m^2).
SOLAR_CONSTANT_W_M2 = 1353.0
#: Clear-sky diffuse irradiance as a fraction of the direct-horizontal component.
CLEAR_SKY_DIFFUSE_FRACTION = 0.10
#: Default isotropic ground-reflected fraction for the plane-of-array transposition.
GROUND_ALBEDO = 0.20


@dataclass(frozen=True)
class ClearSkyIrradiance:
    """Clear-sky components in W/m^2: direct normal, diffuse horizontal, global horizontal."""

    dni: float
    dhi: float
    ghi: float


ZERO_IRRADIANCE = ClearSkyIrradiance(0.0, 0.0, 0.0)


def earth_sun_distance_factor(day_of_year: int) -> float:
    """``1 + 0.033 cos(2 pi n / 365)``: closest in early January, farthest in early July."""
    return 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)


def extraterrestrial_normal_w_m2(day_of_year: int) -> float:
    """The solar constant corrected for the earth-sun distance on ``day_of_year``."""
    return SOLAR_CONSTANT_W_M2 * earth_sun_distance_factor(day_of_year)


def kasten_young_air_mass(zenith_deg: float) -> float:
    """Kasten-Young relative air mass; ``zenith_deg`` at or past the horizon gives the horizon value."""
    zenith = min(max(zenith_deg, 0.0), 90.0)
    return 1.0 / (math.cos(math.radians(zenith)) + 0.50572 * (96.07995 - zenith) ** -1.6364)


def clear_sky_irradiance(
    zenith_deg: float, *, day_of_year: int, elevation_m: float = 0.0
) -> ClearSkyIrradiance:
    """Meinel / Laue direct normal with Kasten-Young air mass, plus the 10 % diffuse.

    Zero below the horizon. ``elevation_m`` is the site elevation above sea
    level for Laue's altitude term; pass 0 when the register does not say.
    """
    if zenith_deg >= 90.0:
        return ZERO_IRRADIANCE
    cos_zenith = math.cos(math.radians(zenith_deg))
    altitude_km = max(elevation_m, 0.0) / 1000.0
    air_mass = kasten_young_air_mass(zenith_deg)
    dni = extraterrestrial_normal_w_m2(day_of_year) * (
        (1.0 - 0.14 * altitude_km) * 0.7 ** (air_mass**0.678) + 0.14 * altitude_km
    )
    dhi = CLEAR_SKY_DIFFUSE_FRACTION * dni * cos_zenith
    return ClearSkyIrradiance(dni=dni, dhi=dhi, ghi=dni * cos_zenith + dhi)


def isotropic_plane_of_array_w_m2(
    irradiance: ClearSkyIrradiance, *, cos_incidence: float, tilt_deg: float, albedo: float = GROUND_ALBEDO
) -> float:
    """Isotropic-sky transposition onto a plane tilted ``tilt_deg`` from horizontal.

    ``cos_incidence`` is the cosine of the angle between the sun and the plane
    normal, as the caller's geometry computes it; a sun behind the plane
    contributes no beam.
    """
    tilt = math.radians(tilt_deg)
    beam = irradiance.dni * max(cos_incidence, 0.0)
    sky = irradiance.dhi * (1.0 + math.cos(tilt)) / 2.0
    ground = irradiance.ghi * albedo * (1.0 - math.cos(tilt)) / 2.0
    return max(beam + sky + ground, 0.0)


__all__ = [
    "CLEAR_SKY_DIFFUSE_FRACTION",
    "GROUND_ALBEDO",
    "SOLAR_CONSTANT_W_M2",
    "ZERO_IRRADIANCE",
    "ClearSkyIrradiance",
    "clear_sky_irradiance",
    "earth_sun_distance_factor",
    "extraterrestrial_normal_w_m2",
    "isotropic_plane_of_array_w_m2",
    "kasten_young_air_mass",
]
