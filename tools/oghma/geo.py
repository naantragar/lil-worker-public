"""MGRS -> lat/lon, stdlib only (WGS84, Krüger series). Patagonia writes station positions as MGRS.

    >>> round(mgrs_to_ll("37TBN8289173794")[0], 3)
    47.64...
"""
from __future__ import annotations

import math
import re

_A, _F = 6378137.0, 1 / 298.257223563
_K0 = 0.9996
_BANDS = "CDEFGHJKLMNPQRSTUVWX"
_ROWS = "ABCDEFGHJKLMNPQRSTUV"
_COLS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
RX = re.compile(r"(\d{1,2})([C-X])\s*([A-Z])([A-Z])\s*(\d{2,10})")


def utm_to_ll(zone: int, easting: float, northing: float) -> tuple[float, float]:
    n = _F / (2 - _F)
    a_ = _A / (1 + n) * (1 + n * n / 4 + n ** 4 / 64)
    b = [0, n / 2 - 2 * n * n / 3 + 37 * n ** 3 / 96, n * n / 48 + n ** 3 / 15, 17 * n ** 3 / 480]
    d = [0, 2 * n - 2 * n * n / 3 - 2 * n ** 3, 7 * n * n / 3 - 8 * n ** 3 / 5, 56 * n ** 3 / 15]
    xi = northing / (_K0 * a_)
    eta = (easting - 500000) / (_K0 * a_)
    xi_ = xi - sum(b[j] * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j in range(1, 4))
    eta_ = eta - sum(b[j] * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j in range(1, 4))
    chi = math.asin(math.sin(xi_) / math.cosh(eta_))
    lat = chi + sum(d[j] * math.sin(2 * j * chi) for j in range(1, 4))
    lon0 = math.radians(zone * 6 - 183)
    lon = lon0 + math.atan2(math.sinh(eta_), math.cos(xi_))
    return math.degrees(lat), math.degrees(lon)


def mgrs_to_ll(s: str, centre: bool = True) -> tuple[float, float] | None:
    """Centre of the MGRS square (a 1 m ref gives that metre, a 1 km ref the middle of the km)."""
    m = RX.fullmatch(re.sub(r"\s+", "", s.upper())) or RX.search(s.upper())
    if not m:
        return None
    zone, band, col, row, digits = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
    if len(digits) % 2:
        return None
    k = len(digits) // 2
    unit = 10 ** (5 - k)
    e_in, n_in = int(digits[:k]) * unit, int(digits[k:]) * unit
    if centre:
        e_in += unit / 2
        n_in += unit / 2
    cols = _COLS[(zone - 1) % 3]
    if col not in cols:
        return None
    easting = (cols.index(col) + 1) * 100000 + e_in
    rows = _ROWS if zone % 2 else _ROWS[5:] + _ROWS[:5]
    if row not in rows:
        return None
    northing = rows.index(row) * 100000 + n_in
    # lift by 2000 km until the point falls into its latitude band
    band_lat = -80 + _BANDS.index(band) * 8
    for _ in range(6):
        lat, lon = utm_to_ll(zone, easting, northing)
        if lat >= band_lat - 0.5:
            return lat, lon
        northing += 2000000
    return None


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        print(a, mgrs_to_ll(a))
