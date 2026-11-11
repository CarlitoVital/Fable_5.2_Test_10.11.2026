"""Clip every raster the project needs to the area of interest.

All sources are cloud-optimised GeoTIFFs on public object storage, so only the
few blocks covering La Paz are transferred (HTTP range requests through GDAL's
/vsicurl/). Results land in ``data/raw`` as small, self-describing GeoTIFFs.

    ghsl_built_s_<year>.tif   built-up surface per 100 m cell, 1975..2020  (JRC GHSL R2023A)
    ghsl_age.tif              dominant built-up epoch per 100 m cell       (JRC GHSL R2025A)
    ghsl_anbh.tif             mean net building height per 100 m cell      (JRC GHSL R2023A)
    wsf_evolution.tif         year of first settlement detection, 30 m     (DLR WSF Evolution)
    obt_<year>.tif            building presence + height, 4 m, 2016..2023  (Google Open Buildings 2.5D Temporal)
    dem.tif                   Copernicus DEM GLO-30
    worldcover.tif            ESA WorldCover 2021 (read at ~20 m)

Usage:  python -m pipeline.fetch_rasters [ghsl] [wsf] [dem] [worldcover] [obt]
"""
from __future__ import annotations

import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import transform as window_transform

from . import config as C

PROFILE = dict(driver="GTiff", compress="deflate", predictor=2, tiled=True,
               blockxsize=256, blockysize=256)


def _write(path, arr, transform, crs, nodata=None, predictor=2):
    arr = np.atleast_3d(arr) if arr.ndim == 2 else arr
    if arr.ndim == 3 and arr.shape[2] == 1 and arr.shape[0] != 1:
        arr = arr.transpose(2, 0, 1)
    prof = dict(PROFILE, count=arr.shape[0], height=arr.shape[1], width=arr.shape[2],
                dtype=arr.dtype, transform=transform, crs=crs, nodata=nodata,
                predictor=predictor)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(arr)
    print(f"  wrote {path.name:28s} {arr.shape[2]}x{arr.shape[1]} {arr.dtype} "
          f"({path.stat().st_size / 1e6:.2f} MB)", flush=True)


def clip(url: str, bounds_lonlat, out_path, pad=0.0, decimate=1,
         resampling=Resampling.nearest):
    """Window-read ``url`` over lon/lat bounds (optionally decimated) and save it."""
    with rasterio.open("/vsicurl/" + url) as ds:
        b = transform_bounds("EPSG:4326", ds.crs, *bounds_lonlat, densify_pts=41)
        b = (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
        win = from_bounds(*b, transform=ds.transform).round_offsets().round_lengths()
        win = win.intersection(Window(0, 0, ds.width, ds.height))
        h, w = int(win.height) // decimate, int(win.width) // decimate
        arr = ds.read(1, window=win, out_shape=(h, w), resampling=resampling)
        tr = window_transform(win, ds.transform)
        tr = tr * tr.scale(win.width / w, win.height / h)
        _write(out_path, arr, tr, ds.crs, ds.nodata,
               predictor=3 if arr.dtype.kind == "f" else 2)
    return out_path


# ----------------------------------------------------------------------------
def fetch_ghsl():
    print("GHSL (JRC) - built-up surface epochs, built-up age, building height")
    pad = 1500  # metres (Mollweide)
    jobs = [(C.GHSL_BUILT_S.format(year=y), C.RAW / f"ghsl_built_s_{y}.tif")
            for y in C.GHSL_EPOCHS]
    jobs += [(C.GHSL_AGE, C.RAW / "ghsl_age.tif"), (C.GHSL_ANBH, C.RAW / "ghsl_anbh.tif")]
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda j: clip(j[0], C.AOI_BUILDINGS, j[1], pad=pad), jobs))


def fetch_wsf():
    print("WSF Evolution (DLR) - year of first settlement detection, 1985-2015")
    w, s, e, n = C.AOI_BUILDINGS
    pad = 0.01
    for lon, lat in C.WSF_EVO_TILES:
        if lon + 2.0 < w - pad or lon > e + pad:  # tile does not reach the AOI
            continue
        if lon >= e - 1e-9:  # AOI ends on the tile edge: the western tile overlaps it
            continue
        clip(C.WSF_EVO.format(lon=lon, lat=lat), C.AOI_BUILDINGS,
             C.RAW / "wsf_evolution.tif", pad=pad)


def _mosaic_1deg(template, bounds, res_deg, name, decimate=1,
                 resampling=Resampling.nearest, step=1):
    """Mosaic lon/lat tiles (``step`` degrees wide, named by SW corner) over bounds."""
    w, s, e, n = bounds
    width = int(round((e - w) / res_deg))
    height = int(round((n - s) / res_deg))
    out, crs, dtype = None, None, None
    lat0 = math.floor(s / step) * step
    lon0 = math.floor(w / step) * step
    for lat in range(lat0, math.ceil(n), step):
        for lon in range(lon0, math.ceil(e), step):
            url = template.format(ns="S" if lat < 0 else "N", lat=abs(lat),
                                  ew="W" if lon < 0 else "E", lon=abs(lon))
            with rasterio.open("/vsicurl/" + url) as ds:
                bw, bs = max(w, lon), max(s, lat)
                be, bn = min(e, lon + step), min(n, lat + step)
                win = from_bounds(bw, bs, be, bn, transform=ds.transform)
                c0 = int(round((bw - w) / res_deg)); c1 = int(round((be - w) / res_deg))
                r0 = int(round((n - bn) / res_deg)); r1 = int(round((n - bs) / res_deg))
                arr = ds.read(1, window=win, out_shape=(r1 - r0, c1 - c0),
                              resampling=resampling, boundless=True)
                if out is None:
                    out = np.zeros((height, width), arr.dtype)
                    crs = ds.crs
                out[r0:r1, c0:c1] = arr
                print(f"    {url.rsplit('/', 1)[1]}  -> rows {r0}:{r1} cols {c0}:{c1}",
                      flush=True)
    _write(C.RAW / name, out, from_origin(w, n, res_deg, res_deg), crs,
           predictor=3 if out.dtype.kind == "f" else 2)


def fetch_dem():
    print("Copernicus DEM GLO-30")
    _mosaic_1deg(C.COP_DEM, C.AOI_TERRAIN, 1 / 3600, "dem.tif",
                 resampling=Resampling.bilinear)


def fetch_worldcover():
    print("ESA WorldCover 2021 (10 m product read at 20 m)")
    _mosaic_1deg(C.WORLDCOVER, C.AOI_TERRAIN, 2 / 12000, "worldcover.tif", step=3)


# ----------------------------------------------------------------------------
# Google Open Buildings 2.5D Temporal
# ----------------------------------------------------------------------------
OBT_RES = 4.0  # metres; the product's effective resolution (files are 0.5 m)


def obt_grid():
    """Target grid (UTM 19S, 4 m, snapped to multiples of 4 m) covering the AOI."""
    x0, y0, x1, y1 = transform_bounds("EPSG:4326", C.CRS_METRIC, *C.AOI_BUILDINGS,
                                      densify_pts=41)
    x0 = math.floor(x0 / OBT_RES) * OBT_RES; y0 = math.floor(y0 / OBT_RES) * OBT_RES
    x1 = math.ceil(x1 / OBT_RES) * OBT_RES; y1 = math.ceil(y1 / OBT_RES) * OBT_RES
    return x0, y0, x1, y1, int((x1 - x0) / OBT_RES), int((y1 - y0) / OBT_RES)


def obt_tiles(year: int):
    """All (url, x0, y0, x1, y1) tiles of one year that touch the AOI."""
    gx0, gy0, gx1, gy1, _, _ = obt_grid()
    names, token = [], None
    while True:
        p = {"prefix": "v1/manifests/", "maxResults": 1000,
             "fields": "items(name),nextPageToken"}
        if token:
            p["pageToken"] = token
        d = requests.get(C.OB_TEMPORAL_API, params=p, timeout=60).json()
        names += [i["name"] for i in d.get("items", [])]
        token = d.get("nextPageToken")
        if not token:
            break
    epsg = C.CRS_METRIC.split(":")[1]
    tiles = []
    for name in names:
        if f"EPSG_{epsg}_{year}_" not in name:
            continue
        man = requests.get(C.OB_TEMPORAL_HTTP + name, timeout=180).json()
        prefix = man["uriPrefix"].replace("gs://open-buildings-temporal-data/",
                                          C.OB_TEMPORAL_HTTP)
        for ts in man["tilesets"]:
            for src in ts["sources"]:
                a, dim = src["affineTransform"], src["dimensions"]
                tx0, ty1 = a["translateX"], a["translateY"]
                tx1 = tx0 + a["scaleX"] * dim["width"]
                ty0 = ty1 + a["scaleY"] * dim["height"]
                if tx0 < gx1 and tx1 > gx0 and ty0 < gy1 and ty1 > gy0:
                    tiles.append((prefix + src["uris"][0], tx0, ty0, tx1, ty1))
    return tiles


def _read_obt_tile(tile):
    url, tx0, ty0, tx1, ty1 = tile
    gx0, gy0, gx1, gy1, gw, gh = obt_grid()
    bx0, by0, bx1, by1 = max(tx0, gx0), max(ty0, gy0), min(tx1, gx1), min(ty1, gy1)
    for attempt in range(4):
        try:
            with rasterio.open("/vsicurl/" + url) as ds:
                names = list(ds.descriptions)
                bands = [names.index("building_presence") + 1,
                         names.index("building_height") + 1]
                win = from_bounds(bx0, by0, bx1, by1, transform=ds.transform)
                shape = (2, int(round((by1 - by0) / OBT_RES)),
                         int(round((bx1 - bx0) / OBT_RES)))
                arr = ds.read(bands, window=win, out_shape=shape,
                              resampling=Resampling.bilinear, masked=True)
            break
        except Exception as exc:  # noqa: BLE001 - network hiccup, retry
            if attempt == 3:
                raise
            print(f"    retry {url.rsplit('/', 1)[1]}: {exc!r}"[:160], flush=True)
            time.sleep(3 * (attempt + 1))
    c0 = int(round((bx0 - gx0) / OBT_RES)); r0 = int(round((gy1 - by1) / OBT_RES))
    return r0, c0, arr


def fetch_obt(years=None):
    print("Google Open Buildings 2.5D Temporal - presence + height at 4 m")
    gx0, gy0, gx1, gy1, gw, gh = obt_grid()
    for year in years or C.OB_TEMPORAL_YEARS:
        out = C.RAW / f"obt_{year}.tif"
        if out.exists():
            print(f"  {out.name} exists, skipping")
            continue
        t0 = time.time()
        tiles = obt_tiles(year)
        presence = np.zeros((gh, gw), np.uint8)       # 0..250 = 0..1, 255 = no data
        presence[:] = 255
        height = np.zeros((gh, gw), np.uint16)        # decimetres
        with ThreadPoolExecutor(4) as ex:
            for r0, c0, arr in ex.map(_read_obt_tile, tiles):
                p, h = arr[0], arr[1]
                valid = ~np.ma.getmaskarray(p)
                pq_ = np.clip(np.round(p.filled(0) * 250), 0, 250).astype(np.uint8)
                hq = np.clip(np.round(h.filled(0) * 10), 0, 65535).astype(np.uint16)
                sl = (slice(r0, r0 + p.shape[0]), slice(c0, c0 + p.shape[1]))
                cur_p, cur_h = presence[sl], height[sl]
                first = valid & (cur_p == 255)
                both = valid & (cur_p != 255)
                cur_p[first] = pq_[first]; cur_h[first] = hq[first]
                cur_p[both] = np.maximum(cur_p[both], pq_[both])
                cur_h[both] = np.maximum(cur_h[both], hq[both])
        tr = from_origin(gx0, gy1, OBT_RES, OBT_RES)
        prof = dict(PROFILE, count=1, height=gh, width=gw, transform=tr,
                    crs=C.CRS_METRIC)
        # both layers go in one uint16 file: band 1 presence, band 2 height
        with rasterio.open(out, "w", **dict(prof, count=2, dtype="uint16")) as dst:
            dst.write(presence.astype(np.uint16), 1)
            dst.write(height, 2)
            dst.set_band_description(1, "building_presence_x250 (255 = no data)")
            dst.set_band_description(2, "building_height_dm")
        print(f"  wrote {out.name}  {gw}x{gh}  {len(tiles)} tiles  "
              f"({out.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.0f}s)", flush=True)


STEPS = dict(ghsl=fetch_ghsl, wsf=fetch_wsf, dem=fetch_dem,
             worldcover=fetch_worldcover, obt=fetch_obt)

if __name__ == "__main__":
    todo = sys.argv[1:] or list(STEPS)
    with rasterio.Env(**C.gdal_env()):
        for step in todo:
            STEPS[step]()
