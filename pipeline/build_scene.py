"""Turn the dated buildings and the terrain into compact arrays for the renderer.

Output (``data/scene/*.npy`` + ``scene.json``), all in a local metric frame:
x = metres east, y = metres north of Plaza Murillo (UTM 19S), z = metres a.s.l.

Terrain   two regular height grids - a fine one (30 m) around the city, a
          coarse one (90 m) for the mountains around it - each with a land
          cover albedo. The fine grid is blended into the coarse one along
          its rim so the two meshes meet without cracks.
Buildings footprints as one flat vertex array with offsets (CSR), a roof
          triangulation, ground level, height and the animation time.
          Sorted by time, so "everything built until t" is a prefix.

Usage:  python -m pipeline.build_scene
"""
from __future__ import annotations

import json
import time

import mapbox_earcut as earcut
import numpy as np
import pyarrow.parquet as pq
import pyproj
import rasterio
import shapely
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import ndimage

from . import config as C

OUT = C.ROOT / "data" / "scene"
FINE, COARSE = 30.0, 90.0            # grid spacing in metres
FINE_MARGIN = 5000.0                 # fine grid reaches this far beyond the buildings
BLEND = 900.0                        # width of the fine -> coarse blend at the rim

# ESA WorldCover class -> dark cartographic albedo (sRGB hex)
LANDCOVER = {
    10: "#1c3226",   # tree cover
    20: "#25352c",   # shrubland
    30: "#29382f",   # grassland (puna)
    40: "#2e3a2d",   # cropland
    50: "#2a3034",   # built-up: neutral, the city is drawn by the buildings
    60: "#33393c",   # bare / sparse vegetation
    70: "#a3b6c4",   # snow and ice
    80: "#13253c",   # permanent water
    90: "#213a37",   # herbaceous wetland (bofedales)
    95: "#213a37",
    100: "#3a4245",  # moss and lichen
    0: "#29382f",
}


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)]) / 255.0


def origin_utm():
    tr = pyproj.Transformer.from_crs("EPSG:4326", C.CRS_METRIC, always_xy=True)
    return tr.transform(*C.ORIGIN_LONLAT)


def warp_to_grid(path, x0, y1, nx, ny, res, resampling, dtype=np.float32):
    """Resample a lon/lat raster onto a UTM grid of *vertices* (x0, y1 = NW vertex)."""
    with rasterio.open(path) as ds:
        dst = np.zeros((ny, nx), dtype)
        # vertices sit at pixel centres of the destination raster
        tr = from_origin(x0 - res / 2, y1 + res / 2, res, res)
        reproject(rasterio.band(ds, 1), dst, dst_transform=tr, dst_crs=C.CRS_METRIC,
                  resampling=resampling)
    return dst


def build_terrain(bx0, by0, bx1, by1, ox, oy):
    """bx*: metric bounding box of the buildings. Returns dict of arrays + metadata."""
    tr = pyproj.Transformer.from_crs("EPSG:4326", C.CRS_METRIC, always_xy=True)
    w, s, e, n = C.AOI_TERRAIN
    xs, ys = tr.transform([w, w, e, e], [s, n, s, n])
    # largest axis-aligned UTM rectangle that is fully inside the lon/lat box
    cx0, cx1 = max(xs[0], xs[1]) + 200, min(xs[2], xs[3]) - 200
    cy0, cy1 = max(ys[0], ys[2]) + 200, min(ys[1], ys[3]) - 200
    snap = lambda v, f: f(v / COARSE) * COARSE
    # snap everything to the coarse lattice (relative to the origin)
    cx0, cy0 = ox + snap(cx0 - ox, np.ceil), oy + snap(cy0 - oy, np.ceil)
    cx1, cy1 = ox + snap(cx1 - ox, np.floor), oy + snap(cy1 - oy, np.floor)
    fx0 = ox + snap(bx0 - FINE_MARGIN - ox, np.floor)
    fy0 = oy + snap(by0 - FINE_MARGIN - oy, np.floor)
    fx1 = ox + snap(bx1 + FINE_MARGIN - ox, np.ceil)
    fy1 = oy + snap(by1 + FINE_MARGIN - oy, np.ceil)
    fx0, fy0, fx1, fy1 = max(fx0, cx0), max(fy0, cy0), min(fx1, cx1), min(fy1, cy1)

    out, meta = {}, {}
    for name, (x0, y0, x1, y1, res) in {"coarse": (cx0, cy0, cx1, cy1, COARSE),
                                        "fine": (fx0, fy0, fx1, fy1, FINE)}.items():
        nx, ny = int(round((x1 - x0) / res)) + 1, int(round((y1 - y0) / res)) + 1
        z = warp_to_grid(C.RAW / "dem.tif", x0, y1, nx, ny, res,
                         Resampling.cubic if res <= 30 else Resampling.average)
        lc = warp_to_grid(C.RAW / "worldcover.tif", x0, y1, nx, ny, res,
                          Resampling.nearest if res <= 30 else Resampling.mode,
                          dtype=np.uint8)
        lut = np.zeros((256, 3), np.float32)
        lut[:] = srgb_to_linear(hex_rgb(LANDCOVER[0]))
        for k, v in LANDCOVER.items():
            lut[k] = srgb_to_linear(hex_rgb(v))
        albedo = lut[lc]
        # soften class boundaries a little - land cover at 10 m is speckly
        sig = 1.2 if res <= 30 else 0.7
        albedo = np.stack([ndimage.gaussian_filter(albedo[..., k], sig) for k in range(3)], -1)
        out[f"{name}_z"], out[f"{name}_albedo"] = z, albedo.astype(np.float32)
        meta[name] = dict(x0=float(x0 - ox), y1=float(y1 - oy), res=res, nx=nx, ny=ny)
        print(f"  terrain {name}: {nx} x {ny} vertices @ {res:.0f} m, "
              f"z {z.min():.0f}..{z.max():.0f} m")

    # blend the rim of the fine grid into the (bilinearly upsampled) coarse grid
    mf, mc = meta["fine"], meta["coarse"]
    k = int(COARSE / FINE)
    ci0 = int(round((mf["x0"] - mc["x0"]) / COARSE))
    cj0 = int(round((mc["y1"] - mf["y1"]) / COARSE))
    ncx, ncy = (mf["nx"] - 1) // k + 1, (mf["ny"] - 1) // k + 1
    sub = out["coarse_z"][cj0:cj0 + ncy, ci0:ci0 + ncx]
    # exact bilinear upsampling of the coarse heights onto the fine vertex lattice
    yi = np.arange(mf["ny"]) / k
    xi = np.arange(mf["nx"]) / k
    up = ndimage.map_coordinates(sub, np.meshgrid(yi, xi, indexing="ij"), order=1,
                                 mode="nearest")
    jj, ii = np.mgrid[0:mf["ny"], 0:mf["nx"]]
    edge = np.minimum.reduce([ii, jj, mf["nx"] - 1 - ii, mf["ny"] - 1 - jj]) * FINE
    wgt = np.clip(1.0 - edge / BLEND, 0.0, 1.0).astype(np.float32)
    wgt = wgt * wgt * (3 - 2 * wgt)
    out["fine_z"] = (out["fine_z"] * (1 - wgt) + up * wgt).astype(np.float32)
    sub_a = out["coarse_albedo"][cj0:cj0 + ncy, ci0:ci0 + ncx]
    up_a = np.stack([ndimage.map_coordinates(sub_a[..., c], np.meshgrid(yi, xi, indexing="ij"),
                                             order=1, mode="nearest") for c in range(3)], -1)
    out["fine_albedo"] = (out["fine_albedo"] * (1 - wgt[..., None])
                          + up_a * wgt[..., None]).astype(np.float32)
    meta["fine"]["coarse_cells"] = [ci0, cj0, ncx - 1, ncy - 1]  # hole in the coarse mesh
    return out, meta


def sample_grid(z, meta, x, y):
    """Bilinear height of a terrain grid at local metric coordinates."""
    c = (x - meta["x0"]) / meta["res"]
    r = (meta["y1"] - y) / meta["res"]
    return ndimage.map_coordinates(z, [r, c], order=1, mode="nearest")


def build_buildings(ox, oy):
    tbl = pq.read_table(C.PROCESSED / "buildings.parquet")
    n = tbl.num_rows
    geom = shapely.from_wkb(tbl["geometry"].to_numpy(zero_copy_only=False))
    t = tbl["t"].to_numpy().astype(np.float32)
    method = tbl["method"].to_numpy()
    year = tbl["year"].to_numpy()
    x = tbl["x"].to_numpy() - ox
    y = tbl["y"].to_numpy() - oy

    # order: 1985 stock (centre outwards, for the opening), then by time, undated last.
    # The distance is jittered per building so the opening spreads like a city,
    # with a ragged edge, rather than like a radar sweep.
    dist = np.hypot(x, y)
    h = (np.floor(x * 3.0).astype(np.int64) * 73856093
         ^ np.floor(y * 3.0).astype(np.int64) * 19349663) & 0xFFFF
    spread = (dist + 1500.0) * (0.55 + 0.9 * h / 65535.0)
    base = (method > 0) & (year <= C.YEAR_BASE)
    und = method == 0
    key_t = np.where(und, 1e9, np.where(base, C.YEAR_BASE, t)).astype(np.float64)
    key_d = np.where(base | und, spread, 0.0)
    order = np.lexsort((key_d, key_t))
    geom, t, method, year, x, y, dist = (a[order] for a in (geom, t, method, year, x, y, dist))
    base, und = base[order], und[order]
    height = tbl["height"].to_numpy()[order].astype(np.float32)
    area = tbl["area"].to_numpy()[order].astype(np.float32)

    # opening sequence: rank of the 1985 stock by distance from Plaza Murillo (0..1)
    intro = np.zeros(n, np.float32)
    nb = int(base.sum())
    intro[base] = (np.arange(nb) + 0.5) / nb
    # the undated get the same treatment for their reveal at the very end
    nu = int(und.sum())
    intro[und] = (np.arange(nu) + 0.5) / max(nu, 1)

    geom = shapely.orient_polygons(geom, exterior_cw=False) if hasattr(
        shapely, "orient_polygons") else shapely.normalize(geom)
    coords, (ring_off, poly_off) = shapely.to_ragged_array(geom)[1:]
    assert len(poly_off) == n + 1 and np.all(np.diff(poly_off) == 1)
    # drop the closing vertex of every ring
    counts = np.diff(ring_off) - 1
    keep = np.ones(len(coords), bool)
    keep[ring_off[1:] - 1] = False
    xy = (coords[keep] - [ox, oy]).astype(np.float32)
    off = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)

    # make every ring counter-clockwise (outward wall normals rely on it)
    seg = np.repeat(np.arange(n), counts)
    nxt = np.arange(len(xy)) + 1
    nxt[off[1:] - 1] = off[:-1]
    cross = xy[:, 0].astype(np.float64) * xy[nxt, 1] - xy[nxt, 0].astype(np.float64) * xy[:, 1]
    signed = np.bincount(seg, weights=cross, minlength=n)
    for b in np.flatnonzero(signed < 0):
        xy[off[b]:off[b + 1]] = xy[off[b]:off[b + 1]][::-1]

    # roofs: fan for triangles/convex quads, ear clipping for the rest
    t0 = time.time()
    roof_counts = np.maximum(counts - 2, 0)
    roof_off = np.concatenate([[0], np.cumsum(roof_counts)]).astype(np.int64)
    roof = np.zeros((int(roof_off[-1]), 3), np.int32)
    simple = counts <= 4
    for b in np.flatnonzero(~simple):
        ring = xy[off[b]:off[b + 1]].astype(np.float64)
        tri = earcut.triangulate_float64(ring, np.array([len(ring)], np.uint32))
        tri = tri.reshape(-1, 3)
        m = min(len(tri), roof_counts[b])
        roof[roof_off[b]:roof_off[b] + m] = tri[:m]
        if m < roof_counts[b]:            # degenerate ring: pad with empty triangles
            roof[roof_off[b] + m:roof_off[b + 1]] = 0
    tri3 = np.flatnonzero(counts == 3)
    roof[roof_off[tri3]] = [0, 1, 2]
    quad = np.flatnonzero(counts == 4)
    # split quads along the diagonal that keeps both triangles inside (handles darts)
    q = xy[off[quad][:, None] + np.arange(4)[None, :]].astype(np.float64)

    def turn(a, b, c):
        return (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    reflex1 = turn(q[:, 0], q[:, 1], q[:, 2]) < 0
    reflex3 = turn(q[:, 2], q[:, 3], q[:, 0]) < 0
    use13 = reflex1 | reflex3            # diagonal 1-3 instead of 0-2
    roof[roof_off[quad]] = np.where(use13[:, None], [0, 1, 3], [0, 1, 2])
    roof[roof_off[quad] + 1] = np.where(use13[:, None], [1, 2, 3], [0, 2, 3])
    print(f"  roofs triangulated in {time.time() - t0:.0f}s "
          f"({(~simple).sum():,} polygons with more than 4 corners)")

    out = dict(
        ring_xy=xy, ring_off=off, roof_idx=roof, roof_off=roof_off,
        cx=x.astype(np.float32), cy=y.astype(np.float32),
        height=height, t=np.where(und, -1.0, t).astype(np.float32),
        intro=intro, area=area, method=method.astype(np.uint8),
        year=year.astype(np.int16),
    )
    stats = dict(n=int(n), n_base=nb, n_dated=int((~und).sum()), n_undated=nu,
                 vertices=int(len(xy)), roof_triangles=int(len(roof)))
    return out, stats, (x.min(), y.min(), x.max(), y.max())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ox, oy = origin_utm()
    print("buildings")
    bld, stats, (bx0, by0, bx1, by1) = build_buildings(ox, oy)
    print("terrain")
    ter, tmeta = build_terrain(bx0 + ox, by0 + oy, bx1 + ox, by1 + oy, ox, oy)
    # ground level under every building, from the same mesh the renderer draws
    ground = sample_grid(ter["fine_z"], tmeta["fine"], bld["cx"], bld["cy"])
    bld["ground"] = ground.astype(np.float32)

    for k, v in {**bld, **ter}.items():
        np.save(OUT / f"{k}.npy", v)
    summary = json.loads((C.PROCESSED / "summary.json").read_text())
    meta = dict(origin_lonlat=C.ORIGIN_LONLAT, origin_utm=[ox, oy], crs=C.CRS_METRIC,
                year_base=C.YEAR_BASE, year_end=C.YEAR_END, terrain=tmeta,
                buildings=stats, per_year=summary["per_year"],
                bbox_buildings=[float(bx0), float(by0), float(bx1), float(by1)])
    (OUT / "scene.json").write_text(json.dumps(meta, indent=1))
    size = sum(f.stat().st_size for f in OUT.iterdir()) / 1e6
    print(f"wrote {OUT} ({size:.0f} MB): {json.dumps(stats)}")


if __name__ == "__main__":
    main()
