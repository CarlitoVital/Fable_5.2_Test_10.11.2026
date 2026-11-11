"""Collect, for every building footprint, the raw evidence used to date it.

Nothing is decided here - this step only *measures*:

    wsf_year      DLR WSF Evolution year at the footprint (0 = not settled by 2015,
                  1985 = "1985 or earlier", 1986..2015 = first detection)
    ghsl_<epoch>  JRC GHSL built-up surface (m2) of the 100 m cell, 1975..2020
    ghsl_age      JRC GHS-AGE class of the cell (1 = before 1975 ... 10 = 2015-2020)
    ghsl_anbh     JRC mean net building height of the cell (m)
    obt_p_<year>  Google Open Buildings 2.5D Temporal: mean building presence
                  inside the footprint, 2016..2023
    obt_h_<year>  ... and the 80th-percentile height inside the footprint (m)
    ground        Copernicus DEM elevation at the centroid (m)

plus geometry facts (metric centroid, area, vertex count, source dataset).
The footprints themselves are written in UTM 19S as WKB.

Usage:  python -m pipeline.sample
"""
from __future__ import annotations

import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyproj
import rasterio
import shapely
from rasterio import features
from scipy import ndimage

from . import config as C

OUT = C.PROCESSED / "samples.parquet"
MIN_AREA = 12.0  # m2 - anything smaller is a shed, a kiosk or detector noise


def load_footprints():
    tbl = pq.read_table(C.RAW / "overture_buildings.parquet")
    geom = shapely.from_wkb(tbl["geometry"].to_numpy(zero_copy_only=False))
    # multipolygons (a handful): keep the largest part
    multi = np.flatnonzero(shapely.get_type_id(geom) == 6)
    for i in multi:
        parts = shapely.get_parts(geom[i])
        geom[i] = parts[np.argmax(shapely.area(parts))]
    # outer ring only: courtyards do not matter at city scale
    geom = shapely.polygons(shapely.get_exterior_ring(geom))
    to_utm = pyproj.Transformer.from_crs("EPSG:4326", C.CRS_METRIC, always_xy=True)
    lonlat_c = shapely.get_coordinates(shapely.centroid(geom))
    geom_m = shapely.transform(
        geom, lambda xy: np.column_stack(to_utm.transform(xy[:, 0], xy[:, 1])))
    geom_m = shapely.make_valid(geom_m)
    bad = np.flatnonzero(shapely.get_type_id(geom_m) != 3)
    for i in bad:  # make_valid may return collections: keep the largest polygon
        parts = [p for p in shapely.get_parts(geom_m[i]) if p.geom_type == "Polygon"]
        geom_m[i] = max(parts, key=lambda p: p.area) if parts else shapely.Polygon()
    area = shapely.area(geom_m)

    sources = tbl["sources"].to_pylist()
    dataset = np.array([s[0]["dataset"] if s else "" for s in sources])
    short = {"Google Open Buildings": "google", "Microsoft ML Buildings": "microsoft",
             "OpenStreetMap": "osm"}
    dataset = np.array([short.get(d, d.lower()) for d in dataset])

    keep = area >= MIN_AREA
    # stay strictly inside the AOI so every raster has data for every building
    w, s, e, n = C.AOI_BUILDINGS
    keep &= (lonlat_c[:, 0] > w) & (lonlat_c[:, 0] < e)
    keep &= (lonlat_c[:, 1] > s) & (lonlat_c[:, 1] < n)
    print(f"footprints: {len(geom):,}  kept (area >= {MIN_AREA:.0f} m2, inside AOI): "
          f"{keep.sum():,}")
    cols = dict(
        id=tbl["id"].to_numpy(zero_copy_only=False)[keep],
        source=dataset[keep],
        osm_height=tbl["height"].to_numpy(zero_copy_only=False)[keep].astype("float32"),
        osm_floors=tbl["num_floors"].to_numpy(zero_copy_only=False)[keep].astype("float32"),
        lon=lonlat_c[keep, 0], lat=lonlat_c[keep, 1],
        area=area[keep].astype("float32"),
    )
    return geom_m[keep], cols


def sample_points(path, xs, ys, band=1, bilinear=False):
    with rasterio.open(path) as ds:
        arr = ds.read(band)
        inv = ~ds.transform
        c, r = inv * (np.asarray(xs), np.asarray(ys))
        if bilinear:
            return ndimage.map_coordinates(arr.astype("float32"), [r - 0.5, c - 0.5],
                                           order=1, mode="nearest")
        r = np.clip(np.floor(r).astype(int), 0, ds.height - 1)
        c = np.clip(np.floor(c).astype(int), 0, ds.width - 1)
        return arr[r, c]


def sample_wsf(lon, lat):
    """WSF Evolution year at each centroid, with a one-pixel tolerance.

    A 30 m settlement pixel rarely lines up with a building outline, so where
    the centroid pixel is empty we accept the nearest settled pixel of the 3x3
    neighbourhood (ties resolved towards the later, i.e. more cautious, year).
    """
    with rasterio.open(C.RAW / "wsf_evolution.tif") as ds:
        arr = ds.read(1).astype(np.int32)
        inv = ~ds.transform
        cf, rf = inv * (lon, lat)
    r = np.clip(np.floor(rf).astype(int), 1, arr.shape[0] - 2)
    c = np.clip(np.floor(cf).astype(int), 1, arr.shape[1] - 2)
    exact = arr[r, c]
    best = exact.copy()
    best_d = np.where(exact > 0, 0.0, np.inf)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            v = arr[r + dr, c + dc]
            d = np.hypot(rf - (r + dr + 0.5), cf - (c + dc + 0.5))
            better = (v > 0) & ((d < best_d - 1e-9) | ((np.abs(d - best_d) < 1e-9) & (v > best)))
            best = np.where(better, v, best)
            best_d = np.where(better, d, best_d)
    return exact, best


def label_raster(geom_m, shape, transform):
    """Rasterise building indices (1-based) on the 4 m Open Buildings grid."""
    t0 = time.time()
    coords, offsets = shapely.to_ragged_array(geom_m)[1:]
    ring_off, poly_off = offsets  # one ring per polygon
    shapes = (
        ({"type": "Polygon",
          "coordinates": [coords[ring_off[poly_off[i]]:ring_off[poly_off[i] + 1]].tolist()]},
         i + 1)
        for i in range(len(geom_m)))
    lab = features.rasterize(shapes, out_shape=shape, transform=transform, fill=0,
                             dtype="int32")
    print(f"  label raster {shape[1]}x{shape[0]} in {time.time() - t0:.0f}s, "
          f"{(lab > 0).mean() * 100:.1f}% of pixels inside a footprint")
    return lab


def obt_stats(geom_m, cx, cy):
    """Per-building presence (mean) and height (80th percentile) for 2016..2023."""
    n = len(geom_m)
    out = {}
    with rasterio.open(C.RAW / f"obt_{C.OB_TEMPORAL_YEARS[0]}.tif") as ds:
        shape, transform = (ds.height, ds.width), ds.transform
    lab = label_raster(geom_m, shape, transform)
    flat = lab.ravel()
    inside = np.flatnonzero(flat)
    labels = flat[inside]
    order = np.argsort(labels, kind="stable")
    inside, labels = inside[order], labels[order]
    counts = np.bincount(labels, minlength=n + 1)[1:]
    starts = np.concatenate([[0], np.cumsum(counts)])
    has_px = counts > 0
    # centroid pixel for footprints too small to catch a pixel centre
    inv = ~transform
    cc, cr = inv * (cx, cy)
    cr = np.clip(np.floor(cr).astype(int), 0, shape[0] - 1)
    cc = np.clip(np.floor(cc).astype(int), 0, shape[1] - 1)
    # position of the 80th percentile inside each building's sorted pixel run
    k80 = starts[:-1] + np.minimum(counts - 1, np.floor(counts * 0.8).astype(int))
    for year in C.OB_TEMPORAL_YEARS:
        with rasterio.open(C.RAW / f"obt_{year}.tif") as ds:
            p_raw, h_raw = ds.read(1), ds.read(2)
        p = np.where(p_raw == 255, np.nan, p_raw / 250.0).astype("float32")
        psum = np.bincount(labels, weights=np.nan_to_num(p.ravel()[inside]),
                           minlength=n + 1)[1:]
        pres = np.where(has_px, psum / np.maximum(counts, 1), p[cr, cc])
        hv = h_raw.ravel()[inside].astype("float32") / 10.0
        o2 = np.lexsort((hv, labels))          # sort by building, then height
        hs = hv[o2]
        h80 = np.where(has_px, hs[np.clip(k80, 0, len(hs) - 1)], h_raw[cr, cc] / 10.0)
        out[f"obt_p_{year}"] = pres.astype("float32")
        out[f"obt_h_{year}"] = h80.astype("float32")
        print(f"  {year}: mean presence {np.nanmean(pres):.3f}, "
              f"median height {np.nanmedian(h80):.1f} m", flush=True)
    out["obt_px"] = counts.astype("int32")
    return out


def main():
    t0 = time.time()
    geom_m, cols = load_footprints()
    cen = shapely.get_coordinates(shapely.centroid(geom_m))
    cx, cy = cen[:, 0], cen[:, 1]
    cols["x"], cols["y"] = cx, cy
    cols["n_vertices"] = (shapely.get_num_coordinates(geom_m) - 1).astype("int16")

    print("WSF Evolution")
    cols["wsf_exact"], cols["wsf_year"] = sample_wsf(cols["lon"], cols["lat"])

    print("GHSL")
    to_moll = pyproj.Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    mx, my = to_moll.transform(cols["lon"], cols["lat"])
    for y in C.GHSL_EPOCHS:
        cols[f"ghsl_{y}"] = sample_points(C.RAW / f"ghsl_built_s_{y}.tif", mx, my)
    cols["ghsl_age"] = sample_points(C.RAW / "ghsl_age.tif", mx, my)
    cols["ghsl_anbh"] = sample_points(C.RAW / "ghsl_anbh.tif", mx, my)

    print("Copernicus DEM")
    cols["ground"] = sample_points(C.RAW / "dem.tif", cols["lon"], cols["lat"],
                                   bilinear=True).astype("float32")

    print("Open Buildings 2.5D Temporal")
    cols.update(obt_stats(geom_m, cx, cy))

    cols["geometry"] = shapely.to_wkb(geom_m)
    C.PROCESSED.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), OUT, compression="zstd")
    print(f"wrote {OUT} ({len(geom_m):,} buildings, {OUT.stat().st_size / 1e6:.1f} MB, "
          f"{time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
