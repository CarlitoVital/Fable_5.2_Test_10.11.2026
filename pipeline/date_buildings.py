"""Give every building a year and a height.

There is no open cadastre with construction dates for La Paz or El Alto, so a
building is dated by **when satellites first saw a settlement at its
location** - not by when the present structure was finished. Three sensors
take over from each other:

1. **DLR WSF Evolution** (Landsat, 30 m, yearly 1985-2015) is the backbone.
   The raw raster is noisy from pixel to pixel (it dates half of the colonial
   centre to the late 1990s), so a building takes the 35th percentile of the
   settled pixels within 90 m: "the year by which a third of the surrounding
   block was detected". This applies to buildings on a settled pixel or
   touching one (the 30 m grid rarely lines up with a footprint). Everything
   detected in the first image year is "1985 or earlier" - the stock the
   animation starts with.
2. **Start-up correction**: WSF's first three years (1986-88) are inflated by
   thin early Landsat coverage. Where JRC's GHS-AGE says the cell was mostly
   built before 1985, such buildings join the "1985 or earlier" stock.
3. **Google Open Buildings 2.5D Temporal** (Sentinel-2, 4 m, yearly
   2016-2023) dates what appeared after WSF ends: a footprint outside the WSF
   settlement mask whose building presence turns from absent to present (and
   stays) gets that year.
4. **JRC GHS-AGE** (100 m, 5-year epochs) is the last resort for buildings that
   exist in 2016 but that WSF never mapped; the year is drawn at random inside
   the epoch.

Buildings without any of that evidence stay undated and are not animated.
Heights come from Open Buildings 2.5D (median of 2021-2023, 80th percentile
inside the footprint), replaced by OpenStreetMap height / floor tags where
those exist.

Usage:  python -m pipeline.date_buildings
"""
from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio

from . import config as C

SAMPLES = C.PROCESSED / "samples.parquet"
OUT = C.PROCESSED / "buildings.parquet"
SUMMARY = C.PROCESSED / "summary.json"

# --- tunables ---------------------------------------------------------------
WSF_RADIUS_PX = 3          # 3 x 30 m = 90 m neighbourhood
WSF_PERCENTILE = 0.35      # "a third of the block is settled"
STARTUP_YEARS = (1986, 1988)
PRESENT = 0.30             # Open Buildings presence that counts as "a building"
ABSENT = 0.25              # ... and as "nothing there yet"
APPEAR = 0.35              # presence required in the year of appearance
MIN_HEIGHT, MAX_HEIGHT = 3.0, 180.0
FLOOR_HEIGHT = 3.0

METHODS = {0: "undated", 1: "wsf", 2: "wsf_neighbourhood", 3: "open_buildings_temporal",
           4: "ghs_age"}


def wsf_local_year(lon, lat):
    """35th percentile of settled WSF years within 90 m, and the own-pixel year."""
    with rasterio.open(C.RAW / "wsf_evolution.tif") as ds:
        wsf = ds.read(1).astype(np.int32)
        c, r = ~ds.transform * (lon, lat)
    R = WSF_RADIUS_PX
    r = np.clip(np.floor(r).astype(int), R, wsf.shape[0] - R - 1)
    c = np.clip(np.floor(c).astype(int), R, wsf.shape[1] - R - 1)
    offs = [(dr, dc) for dr in range(-R, R + 1) for dc in range(-R, R + 1)
            if dr * dr + dc * dc <= R * R + 0.5]
    stack = np.stack([wsf[r + dr, c + dc] for dr, dc in offs], axis=1)
    stack = np.where(stack == 0, 9999, stack)
    stack.sort(axis=1)
    k = (stack < 9999).sum(axis=1)
    idx = np.floor(WSF_PERCENTILE * (np.maximum(k, 1) - 1) + 1e-9).astype(int)
    local = np.where(k > 0, stack[np.arange(len(stack)), idx], 0)
    return local, wsf[r, c]


def hash01(x, y, salt=0):
    """Deterministic pseudo-random number in [0, 1) from metric coordinates."""
    a = np.floor(x * 10).astype(np.int64) * 73856093
    b = np.floor(y * 10).astype(np.int64) * 19349663
    h = (a ^ b ^ (salt * 83492791)) & 0x7FFFFFFF
    h = (h * 1103515245 + 12345) & 0x7FFFFFFF
    return (h % 1000003) / 1000003.0


def main():
    names = pq.read_schema(SAMPLES).names
    tbl = pq.read_table(SAMPLES)
    col = {n: tbl[n].to_numpy(zero_copy_only=False) for n in names if n != "geometry"}
    n = tbl.num_rows
    x, y = col["x"], col["y"]

    local, own = wsf_local_year(col["lon"], col["lat"])
    age = col["ghsl_age"].astype(int)
    pres = np.stack([col[f"obt_p_{yr}"] for yr in C.OB_TEMPORAL_YEARS], axis=1)
    pres = np.nan_to_num(pres, nan=0.0)
    years_obt = np.array(C.OB_TEMPORAL_YEARS)

    # Open Buildings: first year a footprint turns from absent to present and
    # stays. Yearly presence is noisy for small buildings, so the rule is strict:
    # every earlier year clearly absent, the year itself clearly present, and
    # present on average ever after. (Run on buildings that certainly predate
    # 1985 it still fires for ~8 % - which is why it is only used outside the
    # WSF settlement mask, where a late date is plausible to begin with.)
    appear = np.zeros(n, int)
    for j in range(1, pres.shape[1]):
        hit = ((appear == 0) & (pres[:, j] >= APPEAR)
               & (pres[:, j:].mean(axis=1) >= PRESENT)
               & (pres[:, :j].max(axis=1) < ABSENT))
        appear[hit] = years_obt[j]
    there_2016 = pres[:, 0] >= PRESENT

    year = np.zeros(n, int)
    method = np.zeros(n, np.uint8)

    # 1. WSF Evolution, own pixel settled
    m = own > 0
    year[m], method[m] = local[m], 1
    # 3. not settled in WSF, but the building appears in Open Buildings 2017-2023
    m = (own == 0) & (appear > 0)
    year[m], method[m] = appear[m], 3
    # 1b. own pixel empty but a settled pixel touches it (<= 45 m): the 30 m grid
    #     simply does not line up with the footprint
    m = (method == 0) & (col["wsf_year"] > 0)
    year[m], method[m] = local[m], 2
    # 4. standing in 2016, never mapped by WSF: JRC epoch, random year inside it
    epochs = np.array([0] + C.GHSL_EPOCHS)
    m = (method == 0) & there_2016 & (age >= 4)
    lo = epochs[np.clip(age - 1, 0, 10)]
    span = epochs[np.clip(age, 0, 10)] - lo
    drawn = lo + 1 + np.floor(hash01(x, y, 7) * span).astype(int)
    year[m], method[m] = np.minimum(drawn[m], 2016), 4

    # 2. start-up correction of the first WSF years
    m = ((method == 1) | (method == 2)) & (year >= STARTUP_YEARS[0]) \
        & (year <= STARTUP_YEARS[1]) & (age >= 1) & (age <= 3)
    year[m] = C.YEAR_BASE
    startup_moved = int(m.sum())

    dated = method > 0
    year[dated] = np.clip(year[dated], C.YEAR_BASE, C.YEAR_END)

    # animation time: base stock at t = YEAR_BASE, everything else spread over its year
    t = np.where(year > C.YEAR_BASE, year + hash01(x, y, 1), float(C.YEAR_BASE))
    t = np.where(dated, t, 0.0).astype("float32")

    # ---- heights -------------------------------------------------------------
    h_obt = np.median(np.stack([col[f"obt_h_{yr}"] for yr in (2021, 2022, 2023)], 1), 1)
    height = np.clip(np.nan_to_num(h_obt, nan=MIN_HEIGHT), MIN_HEIGHT, MAX_HEIGHT)
    h_src = np.full(n, "open_buildings_2.5d", dtype=object)
    floors, tagged = col["osm_floors"], col["osm_height"]
    m = np.isfinite(floors) & (floors > 0)
    height[m] = np.clip(floors[m] * FLOOR_HEIGHT, MIN_HEIGHT, MAX_HEIGHT); h_src[m] = "osm_floors"
    m = np.isfinite(tagged) & (tagged > 0)
    height[m] = np.clip(tagged[m], MIN_HEIGHT, MAX_HEIGHT); h_src[m] = "osm_height"

    out = pa.table({
        "id": col["id"], "source": col["source"],
        "x": x, "y": y, "lon": col["lon"], "lat": col["lat"],
        "area": col["area"], "ground": col["ground"],
        "year": year.astype("int16"), "t": t, "method": method,
        "height": height.astype("float32"), "height_source": h_src.astype(str),
        "geometry": tbl["geometry"],
    })
    pq.write_table(out, OUT, compression="zstd")

    # ---- summary ---------------------------------------------------------------
    per_year = np.bincount(year[dated] - C.YEAR_BASE,
                           minlength=C.YEAR_END - C.YEAR_BASE + 1)
    summary = {
        "footprints": int(n),
        "dated": int(dated.sum()),
        "undated": int((~dated).sum()),
        "base_year": C.YEAR_BASE, "end_year": C.YEAR_END,
        "base_stock": int(per_year[0]),
        "added_after_base": int(per_year[1:].sum()),
        "startup_correction_moved": startup_moved,
        "by_method": {METHODS[k]: int((method == k).sum()) for k in METHODS},
        "per_year": {str(C.YEAR_BASE + i): int(v) for i, v in enumerate(per_year)},
        "height_source": {k: int((h_src == k).sum()) for k in np.unique(h_src)},
        "parameters": dict(wsf_radius_m=WSF_RADIUS_PX * 30, wsf_percentile=WSF_PERCENTILE,
                           startup_years=list(STARTUP_YEARS), present=PRESENT,
                           absent=ABSENT, appear=APPEAR),
    }
    SUMMARY.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_year"}, indent=1))
    print("per year:", per_year.tolist())
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
