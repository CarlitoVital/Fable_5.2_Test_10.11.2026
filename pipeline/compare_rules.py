"""Why the WSF dating rule looks the way it does: three candidates, four tests.

    python -m pipeline.compare_rules

1. "percentile" - every building takes the 35th percentile of the settled
   years within 90 m (the first version of this project).
2. "own pixel"  - the building's own pixel year, except in old fabric (a third
   of the surrounding pixels settled by 1985), which counts as 1985.
3. "frontier"   - like 2, but inside built-up land the year is held to the
   35th-65th percentile of the neighbours'; on the growth frontier (>= 30 %
   of the surroundings still empty) the own year stands. This is the rule in use.

Tests
  JRC agreement  per 100 m cell: share of footprint area dated by 1990 / 2000
                 versus the share of 2020's built-up surface JRC GHSL shows
                 then (area-weighted RMSE; GHSL observed those years with
                 Landsat, independently of WSF - though from similar imagery)
  2011-16        buildings per year at the end of the Landsat record, where
                 an early-biased rule empties the histogram
  speckle        share of post-1985 buildings more than 5 years away from the
                 median of their 10 nearest dated neighbours
  old districts  the known-neighbourhood test of ``pipeline.validate``
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pyproj
import rasterio
from scipy.spatial import cKDTree

from . import config as C
from . import date_buildings as D
from .validate import PLACES, RADIUS

RULES = {
    "percentile": lambda src, nb: nb["lo"],
    "own pixel": lambda src, nb: np.where(nb["old_share"] >= D.OLD_FABRIC_SHARE,
                                          C.YEAR_BASE, src),
    "frontier": D.wsf_year,
}


def main():
    names = [n for n in pq.read_schema(C.PROCESSED / "samples.parquet").names if n != "geometry"]
    s = pq.read_table(C.PROCESSED / "samples.parquet", columns=names).to_pandas()
    own, nb = D.wsf_evidence(s.lon.to_numpy(), s.lat.to_numpy())
    pres = np.nan_to_num(np.stack([s[f"obt_p_{y}"] for y in C.OB_TEMPORAL_YEARS], 1))
    appear = D.first_appearance(pres, np.array(C.OB_TEMPORAL_YEARS))
    x, y = s.x.to_numpy(), s.y.to_numpy()
    args = (own, s.wsf_year.to_numpy(), nb, appear, pres[:, 0] >= D.PRESENT,
            s.ghsl_age.to_numpy().astype(int), D.hash01(x, y, 7))

    # GHSL cell of every building
    moll = pyproj.Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    mx, my = moll.transform(s.lon.to_numpy(), s.lat.to_numpy())
    with rasterio.open(C.RAW / "ghsl_built_s_2020.tif") as ds:
        cc, rr = ~ds.transform * (mx, my)
        cell = np.floor(rr).astype(int) * ds.width + np.floor(cc).astype(int)
    area = s.area.to_numpy().astype(float)
    # neighbours and places
    _, nn = cKDTree(np.column_stack([x, y])).query(np.column_stack([x, y]), k=11)
    tm = pyproj.Transformer.from_crs("EPSG:4326", C.CRS_METRIC, always_xy=True)
    places = []
    for name, lat, lon, _, old in PLACES:
        px, py = tm.transform(lon, lat)
        places.append((((x - px) ** 2 + (y - py) ** 2) < RADIUS ** 2, old))

    rows = []
    for name, rule in RULES.items():
        year, method, _ = D.assign_years(*args, rule=rule)
        dated = (method > 0) & (year <= 2020)
        df = pd.DataFrame({"cell": cell[dated], "a": area[dated], "y": year[dated],
                           "g1990": s.ghsl_1990.to_numpy()[dated],
                           "g2000": s.ghsl_2000.to_numpy()[dated],
                           "g2020": s.ghsl_2020.to_numpy()[dated]})
        g = df.groupby("cell")
        tot, g2020 = g.a.sum(), g.g2020.first().astype(float)
        ok = (g.size() >= 3) & (g2020 > 500)
        rmse = []
        for e in (1990, 2000):
            ours = df[df.y <= e].groupby("cell").a.sum().reindex(tot.index, fill_value=0) / tot
            theirs = (g[f"g{e}"].first() / g2020.clip(lower=1)).clip(upper=1)
            d = (ours - theirs)[ok]
            rmse.append(np.sqrt(np.average(d ** 2, weights=g2020[ok])))
        per = np.bincount(year[method > 0] - C.YEAR_BASE, minlength=C.YEAR_END - C.YEAR_BASE + 1)
        tail = per[2011 - C.YEAR_BASE:2017 - C.YEAR_BASE]
        yy = np.where(method > 0, year, np.nan).astype(float)
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                med = np.nanmedian(yy[nn[:, 1:]], axis=1)
        late = (method > 0) & (year > C.YEAR_BASE) & np.isfinite(med)
        speckle = np.mean(np.abs(yy[late] - med[late]) > 5)
        passed = 0
        for m, old in places:
            share = (year[m & (method > 0)] <= C.YEAR_BASE).mean()
            passed += (share >= 0.85) if old else (share <= 0.5)
        rows.append((name, *rmse, " ".join(f"{v / 1000:.1f}k" for v in tail), speckle,
                     f"{passed}/{len(places)}"))

    print(f"{'rule':12s} {'JRC 1990':>9s} {'JRC 2000':>9s}   {'buildings 2011-16':34s} "
          f"{'speckle':>7s}  old districts")
    for name, r90, r00, tail, sp, ok in rows:
        print(f"{name:12s} {r90:9.3f} {r00:9.3f}   {tail:34s} {sp:7.1%}  {ok}")


if __name__ == "__main__":
    main()
