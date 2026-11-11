"""Sanity-check the building years against neighbourhoods whose age is known.

This is the test that decided the dating rules: a method that puts the
colonial centre in the 1990s, or a 1960s housing estate after 1985, is wrong
no matter how good its global accuracy figures are.

The reference periods are approximate, taken from the general urban history of
La Paz and El Alto (only Villa Adela's start was looked up: its neighbourhood
council dates from 1973); the points are approximate neighbourhood centres.
The test is deliberately coarse - "was this standing by 1985 or not?" - and
anyone who knows the city is invited to correct both.

Usage:  python -m pipeline.validate
"""
from __future__ import annotations

import numpy as np
import pyarrow.parquet as pq
import pyproj

from . import config as C

RADIUS = 400.0  # metres around each point

# name, lat, lon, when it was built up, should it be part of the 1985 stock?
PLACES = [
    ("Plaza Murillo (historic centre)", -16.4957, -68.1335, "colonial", True),
    ("Sopocachi", -16.5100, -68.1280, "early 20th c.", True),
    ("Miraflores", -16.5000, -68.1200, "1920s-50s", True),
    ("Villa Fatima", -16.4800, -68.1170, "1950s-70s", True),
    ("Calacoto (Zona Sur)", -16.5400, -68.0850, "1950s-70s", True),
    ("El Alto - Villa Dolores", -16.5100, -68.1600, "1940s-60s", True),
    ("El Alto - 16 de Julio", -16.4920, -68.1700, "1950s-70s", True),
    ("El Alto - Ciudad Satelite", -16.5270, -68.1520, "1960s-70s", True),
    ("El Alto - Villa Adela", -16.5330, -68.2030, "from 1973", True),
    ("El Alto - Rio Seco", -16.4780, -68.2080, "1980s-90s", False),
    ("Chasquipampa (Zona Sur)", -16.5460, -68.0450, "1980s-2000s", False),
    ("El Alto - Senkata", -16.5780, -68.1850, "1990s-2010s", False),
    ("El Alto - Ventilla", -16.6130, -68.1900, "2000s on", False),
]


def table():
    b = pq.read_table(C.PROCESSED / "buildings.parquet",
                      columns=["x", "y", "year", "method"]).to_pandas()
    tr = pyproj.Transformer.from_crs("EPSG:4326", C.CRS_METRIC, always_xy=True)
    rows = []
    for name, lat, lon, period, old in PLACES:
        x, y = tr.transform(lon, lat)
        near = ((b.x - x) ** 2 + (b.y - y) ** 2) < RADIUS ** 2
        dated = b[near & (b.method > 0)]
        stock = float((dated.year <= C.YEAR_BASE).mean()) if len(dated) else float("nan")
        later = dated.year[dated.year > C.YEAR_BASE]
        median = int(np.median(later)) if len(later) else None
        ok = stock >= 0.85 if old else stock <= 0.5
        rows.append((name, period, int(near.sum()), stock, median, ok))
    return rows


def cross_checks():
    """Three numbers that say how far the sources can be trusted."""
    from . import date_buildings as D
    names = [n for n in pq.read_schema(C.PROCESSED / "samples.parquet").names if n != "geometry"]
    s = pq.read_table(C.PROCESSED / "samples.parquet", columns=names).to_pandas()
    b = pq.read_table(C.PROCESSED / "buildings.parquet",
                      columns=["year", "method", "height"]).to_pandas()
    out = {}
    # 1. do DLR and JRC agree on what stood in the 1980s?
    stock = ((b.method > 0) & (b.year <= C.YEAR_BASE)).to_numpy()
    share_1990 = s.ghsl_1990.to_numpy() / np.maximum(s.ghsl_2020.to_numpy(), 1)
    out["stock_confirmed_by_ghsl_1990"] = float((share_1990[stock] >= 0.15).mean())
    # 2. are the satellite heights any good? compare with OpenStreetMap floor counts
    h_obt = np.median(np.stack([s[f"obt_h_{y}"] for y in (2021, 2022, 2023)], 1), 1)
    f = s.osm_floors.to_numpy()
    m = np.isfinite(f) & (f > 0)
    out["height_vs_osm_floors_r"] = float(np.corrcoef(f[m] * D.FLOOR_HEIGHT, h_obt[m])[0, 1])
    tall = m & (f >= 10)
    out["towers_osm_mean_m"] = float((f[tall] * D.FLOOR_HEIGHT).mean())
    out["towers_open_buildings_mean_m"] = float(h_obt[tall].mean())
    out["towers_n"] = int(tall.sum())
    # 3. how often does the "new building" rule fire on buildings that are certainly old?
    pres = np.nan_to_num(np.stack([s[f"obt_p_{y}"] for y in C.OB_TEMPORAL_YEARS], 1))
    old = (s.wsf_exact.to_numpy() == C.YEAR_BASE)
    appear = D.first_appearance(pres[old], np.array(C.OB_TEMPORAL_YEARS))
    out["appearance_rule_false_positive_rate"] = float((appear > 0).mean())
    return out


def main():
    rows = table()
    print(f"{'neighbourhood':34s} {'built up':13s} {'buildings':>9s} "
          f"{'by 1985':>8s} {'median of the rest':>19s}")
    for name, period, n, stock, median, ok in rows:
        print(f"{name:34s} {period:13s} {n:9d} {stock:8.0%} {str(median or '-'):>19s}"
              f"   {'ok' if ok else 'CHECK'}")
    bad = [r[0] for r in rows if not r[5]]
    print("\nall plausible" if not bad else f"\nimplausible: {bad}")
    c = cross_checks()
    print(f"""
cross-checks
  1985 stock confirmed by JRC GHSL (cell >= 15 % built by 1990): {c['stock_confirmed_by_ghsl_1990']:.1%}
  Open Buildings height vs OSM floor tags (x {3} m):             r = {c['height_vs_osm_floors_r']:.2f}
  towers with >= 10 tagged floors (n = {c['towers_n']}):                  {c['towers_osm_mean_m']:.1f} m tagged, {c['towers_open_buildings_mean_m']:.1f} m measured
  "new building" rule firing on pre-1985 buildings:              {c['appearance_rule_false_positive_rate']:.1%}""")
    return not bad


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
