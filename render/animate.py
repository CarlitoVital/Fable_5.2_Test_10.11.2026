"""Render the La Paz / El Alto growth animation.

    python -m render.animate                 full video  -> out/la_paz_el_alto_growth.mp4
    python -m render.animate --year 2005     one still, end of that year
    python -m render.animate --still 12.5    one still, 12.5 s into the clip
    python -m render.animate --preview       quick half-resolution draft

Timeline: empty landscape -> the city of 1985 rises (centre outwards) ->
1986..2023, every building appearing in its year with a short glow ->
undated buildings fade in grey -> hold.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from numba import njit
from PIL import Image
from scipy import ndimage

from . import config as cfg
from . import raster as R
from .overlay import Overlay, Ramp, composite, hex_rgb, srgb_to_linear


# ----------------------------------------------------------------------------
# timeline
# ----------------------------------------------------------------------------
class Timeline:
    def __init__(self, year_base: int, year_end: int):
        self.year_base, self.year_end = year_base, year_end
        self.t_stock = cfg.T_OPEN
        self.t_years = self.t_stock + cfg.T_STOCK + cfg.T_STOCK_HOLD
        self.t_reveal = self.t_years + (year_end - year_base) * cfg.T_PER_YEAR + 0.4
        self.t_end = self.t_reveal + cfg.T_REVEAL + cfg.T_HOLD

    def year_pos(self, tau: float) -> float:
        """Continuous position on the year axis (1986.0 = start of 1986)."""
        return self.year_base + 1 + (tau - self.t_years) / cfg.T_PER_YEAR

    def tau_of(self, year_pos: float) -> float:
        return self.t_years + (year_pos - self.year_base - 1) * cfg.T_PER_YEAR


# ----------------------------------------------------------------------------
# scene
# ----------------------------------------------------------------------------
class Scene:
    def __init__(self):
        self.meta = json.loads((cfg.SCENE / "scene.json").read_text())
        load = lambda n: np.ascontiguousarray(np.load(cfg.SCENE / f"{n}.npy"))
        for name in ("ring_xy", "ring_off", "roof_idx", "roof_off", "ground", "height",
                     "t", "intro", "method", "cx", "cy",
                     "coarse_z", "coarse_albedo", "fine_z", "fine_albedo"):
            setattr(self, name, load(name))
        b = self.meta["buildings"]
        self.n, self.n_stock, self.n_dated, self.n_undated = (
            b["n"], b["n_base"], b["n_dated"], b["n_undated"])
        self.per_year = {int(k): v for k, v in self.meta["per_year"].items()}
        self.timeline = tl = Timeline(self.meta["year_base"], self.meta["year_end"])

        # birth time of every building on the animation clock (seconds)
        tau = np.empty(self.n, np.float64)
        s, d = self.n_stock, self.n_dated
        tau[:s] = tl.t_stock + self.intro[:s] * cfg.T_STOCK
        tau[s:d] = tl.tau_of(self.t[s:d].astype(np.float64))
        tau[d:] = tl.t_reveal + self.intro[d:] * cfg.T_REVEAL
        assert np.all(np.diff(tau[:s]) >= 0) and np.all(np.diff(tau[s:d]) >= -1e-6)
        self.tau = tau

        # final colour: year ramp with a little per-building variation
        ramp = Ramp()
        rgb = ramp.linear(np.where(self.t > 0, self.t, tl.year_base))
        h = (np.floor(self.cx * 7.0).astype(np.int64) * 73856093
             ^ np.floor(self.cy * 7.0).astype(np.int64) * 19349663) & 0xFFFF
        jitter = 0.86 + 0.28 * (h / 65535.0)
        rgb = rgb * jitter[:, None]
        rgb[d:] = srgb_to_linear(np.array(hex_rgb(cfg.UNDATED_COLOR)) / 255.0) * jitter[d:, None]
        self.final_rgb = rgb.astype(np.float32)
        flash = np.ones(self.n, np.float32)
        flash[:s] = 0.22          # the old city only shimmers as it rises
        flash[d:] = 0.0           # undated: no glow
        self.flash_w = flash
        self.flash_rgb = srgb_to_linear(np.array(hex_rgb(cfg.FLASH_COLOR)) / 255.0).astype(np.float32)


# ----------------------------------------------------------------------------
# post-processing kernels
# ----------------------------------------------------------------------------
@njit(cache=True, fastmath=True)
def resolve(color, emis, depth, S, out_rgb, out_glow, out_depth):
    """Box-filter the supersampled buffers down to output resolution."""
    H, W = out_depth.shape
    inv = 1.0 / (S * S)
    for y in range(H):
        for x in range(W):
            r = 0.0; g = 0.0; b = 0.0; gl = 0.0; dp = 0.0
            for j in range(S):
                yy = y * S + j
                for i in range(S):
                    xx = x * S + i
                    cr = color[yy, xx, 0]; cg = color[yy, xx, 1]; cb = color[yy, xx, 2]
                    r += cr; g += cg; b += cb
                    gl += emis[yy, xx] * (0.30 * cr + 0.59 * cg + 0.11 * cb)
                    dp += depth[yy, xx]
            out_rgb[y, x, 0] = r * inv
            out_rgb[y, x, 1] = g * inv
            out_rgb[y, x, 2] = b * inv
            out_glow[y, x] = gl * inv
            out_depth[y, x] = dp * inv


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


# ----------------------------------------------------------------------------
# renderer
# ----------------------------------------------------------------------------
class Renderer:
    def __init__(self, scene: Scene, width=cfg.WIDTH, height=cfg.HEIGHT, ss=cfg.SUPERSAMPLE):
        self.sc = scene
        self.w, self.h, self.ss = width, height, ss
        self.W, self.H = width * ss, height * ss
        self.color = np.zeros((self.H, self.W, 3), np.float32)
        self.depth = np.zeros((self.H, self.W), np.float32)
        self.emis = np.zeros((self.H, self.W), np.float32)
        self.static_cam = cfg.CAMERA_START == cfg.CAMERA_END
        self.base = None            # baked buffers for a locked-off camera
        self.n_baked = 0
        self.out_rgb = np.zeros((height, width, 3), np.float32)
        self.out_glow = np.zeros((height, width), np.float32)
        self.out_depth = np.zeros((height, width), np.float32)
        self.bg = srgb_to_linear(np.array(hex_rgb(cfg.BACKGROUND)) / 255.0).astype(np.float32)
        self.fog = srgb_to_linear(np.array(hex_rgb(cfg.FOG_COLOR)) / 255.0).astype(np.float32)
        self.ones = np.ones(scene.n, np.float32)
        self.zeros = np.zeros(scene.n, np.float32)
        self._light()
        self._terrain_colors()
        self._masks()
        self.lut = (np.clip(np.where(
            (v := np.linspace(0, 1, 4096)) <= 0.0031308, v * 12.92,
            1.055 * v ** (1 / 2.4) - 0.055), 0, 1) * 255 + 0.5).astype(np.uint8)

    # -- setup -----------------------------------------------------------------
    def _light(self):
        """World-space light, fixed for the whole clip (upper left of the first frame)."""
        az = math.radians(cfg.CAMERA_START["azimuth"] + cfg.LIGHT_AZIMUTH_FROM_CAMERA)
        el = math.radians(cfg.LIGHT_ELEVATION)
        # camera at azimuth a looks along (sin a, cos a); the light comes from behind-left
        self.light = np.array([-math.sin(az) * math.cos(el) * -1.0,
                               math.cos(az) * math.cos(el), math.sin(el)])
        # i.e. towards the light: rotate the view direction by the offset, keep it "far side"
        self.light[0] = math.sin(az) * math.cos(el)
        h = math.hypot(self.light[0], self.light[1])
        self.lx, self.ly = self.light[0] / h, self.light[1] / h

    def _shade(self, z, albedo, res):
        zz = z.astype(np.float64) * cfg.TERRAIN_EXAGGERATION
        gy, gx = np.gradient(zz, res)
        nx, ny, nz = -gx, gy, np.ones_like(gx)          # rows run north -> south
        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        lam = np.clip((nx * self.light[0] + ny * self.light[1] + nz * self.light[2]) / norm, 0, 1)
        shade = cfg.TERRAIN_AMBIENT + cfg.TERRAIN_DIFFUSE * lam
        if cfg.TERRAIN_OCCLUSION > 0:
            # valleys darker, ridges lighter: height relative to its 1.5 km surroundings
            rel = z - ndimage.gaussian_filter(z, 1500.0 / res)
            shade *= 1.0 + cfg.TERRAIN_OCCLUSION * 0.5 * np.tanh(rel / 220.0)
        return (albedo * (shade * cfg.TERRAIN_GAIN)[..., None]).astype(np.float32)

    def _terrain_colors(self):
        t = self.sc.meta["terrain"]
        self.coarse_rgb = self._shade(self.sc.coarse_z, self.sc.coarse_albedo, t["coarse"]["res"])
        self.fine_rgb = self._shade(self.sc.fine_z, self.sc.fine_albedo, t["fine"]["res"])
        self.hole = np.array(t["fine"]["coarse_cells"], np.int64)
        self.no_hole = np.zeros(4, np.int64)

    def _masks(self):
        h, w = self.h, self.w
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        u, v = (xx / w - 0.5) * 2, (yy / h - 0.5) * 2
        r = np.sqrt((u * 0.95) ** 2 + (v * 0.85) ** 2)
        mask = 1.0 - cfg.VIGNETTE * smoothstep((r - 0.45) / 0.95)
        # calm the areas that carry lettering
        mask *= 1.0 - 0.42 * smoothstep((yy / h - 0.74) / 0.26)
        mask *= 1.0 - 0.30 * smoothstep((0.20 - yy / h) / 0.20)
        self.mask = mask.astype(np.float32)

    # -- camera ----------------------------------------------------------------
    def camera(self, tau: float):
        a, b = cfg.CAMERA_START, cfg.CAMERA_END
        u = float(smoothstep(np.clip(tau / self.sc.timeline.t_end, 0, 1)))
        lerp = lambda p, q: p + (q - p) * u
        tgt = [lerp(a["target"][k], b["target"][k]) for k in range(3)]
        tgt[2] = (tgt[2] - cfg.Z_REF) * cfg.TERRAIN_EXAGGERATION
        M, eye = R.orbit_camera(tgt, lerp(a["azimuth"], b["azimuth"]),
                                lerp(a["elevation"], b["elevation"]),
                                lerp(a["distance"], b["distance"]),
                                lerp(a["fov"], b["fov"]), self.w / self.h)
        return np.ascontiguousarray(M), eye

    # -- drawing ---------------------------------------------------------------
    def _draw_terrain(self, color, depth, emis, M):
        color[:] = self.bg
        depth[:] = 0.0
        emis[:] = 0.0
        t = self.sc.meta["terrain"]
        z = cfg.Z_REF, cfg.TERRAIN_EXAGGERATION
        R.draw_grid(color, depth, emis, M, self.W, self.H, t["coarse"]["x0"], t["coarse"]["y1"],
                    t["coarse"]["res"], self.sc.coarse_z, self.coarse_rgb, z[0], z[1], 1.0, self.hole)
        R.draw_grid(color, depth, emis, M, self.W, self.H, t["fine"]["x0"], t["fine"]["y1"],
                    t["fine"]["res"], self.sc.fine_z, self.fine_rgb, z[0], z[1], 1.0, self.no_hole)

    def _draw_buildings(self, color, depth, emis, M, b0, b1, grow, rgb, glow):
        if b1 <= b0:
            return
        sc = self.sc
        R.draw_buildings(color, depth, emis, M, self.W, self.H,
                         sc.ring_xy, sc.ring_off, sc.roof_idx, sc.roof_off, sc.ground, sc.height,
                         b0, b1, grow, rgb, glow,
                         cfg.Z_REF, cfg.TERRAIN_EXAGGERATION, cfg.BUILDING_EXAGGERATION,
                         cfg.SKIRT * cfg.TERRAIN_EXAGGERATION, 1.0, self.lx, self.ly,
                         cfg.WALL_AMBIENT, cfg.WALL_DIFFUSE, cfg.WALL_FOOT, cfg.ROOF_GAIN,
                         cfg.MIN_BUILDING_PX * self.ss / 3.0)

    def _state(self, tau, b0, b1):
        """grow / colour / glow of buildings b0..b1 at clock time tau (full-size arrays)."""
        sc = self.sc
        grow = self.zeros.copy()
        glow = self.zeros.copy()
        rgb = sc.final_rgb.copy()
        age = (tau - sc.tau[b0:b1]).astype(np.float32)
        x = np.clip(age / cfg.RISE, 0, 1)
        grow[b0:b1] = np.where(age >= 0, 1 - (1 - x) ** 3, 0.0)          # ease-out cubic
        f = np.where(age >= 0, np.exp(-np.maximum(age, 0) / cfg.FLASH), 0.0) * sc.flash_w[b0:b1]
        glow[b0:b1] = f
        m = (cfg.FLASH_MIX * f)[:, None]
        rgb[b0:b1] = sc.final_rgb[b0:b1] * (1 - m) + sc.flash_rgb * m
        return grow, rgb, glow

    def render(self, tau: float) -> np.ndarray:
        """3D frame at clock time ``tau`` as sRGB uint8 (h, w, 3), no lettering."""
        sc = self.sc
        M, _ = self.camera(tau)
        n_born = int(np.searchsorted(sc.tau, tau, side="right"))
        if self.static_cam:
            n_settle = int(np.searchsorted(sc.tau, tau - cfg.LIFE, side="right"))
            if self.base is None or n_settle < self.n_baked:
                self.base = (np.empty_like(self.color), np.empty_like(self.depth),
                             np.empty_like(self.emis))
                self._draw_terrain(*self.base, M)
                self.n_baked = 0
            if n_settle > self.n_baked:
                self._draw_buildings(*self.base, M, self.n_baked, n_settle,
                                     self.ones, sc.final_rgb, self.zeros)
                self.n_baked = n_settle
            np.copyto(self.color, self.base[0])
            np.copyto(self.depth, self.base[1])
            np.copyto(self.emis, self.base[2])
            first = n_settle
        else:
            self._draw_terrain(self.color, self.depth, self.emis, M)
            first = 0
        grow, rgb, glow = self._state(tau, first, n_born)
        self._draw_buildings(self.color, self.depth, self.emis, M, first, n_born, grow, rgb, glow)
        return self._post()

    def _post(self) -> np.ndarray:
        resolve(self.color, self.emis, self.depth, self.ss, self.out_rgb, self.out_glow,
                self.out_depth)
        rgb = self.out_rgb
        # distance haze: the far ranges sink into the background
        with np.errstate(divide="ignore"):
            dist = np.where(self.out_depth > 1e-9, 1.0 / self.out_depth, 1e9)
        f = cfg.FOG_STRENGTH * (1 - np.exp(-np.maximum(dist - cfg.FOG_START, 0) / cfg.FOG_LENGTH))
        f = f[..., None].astype(np.float32)
        rgb = rgb * (1 - f) + self.fog * f
        if cfg.BLOOM > 0:
            k = self.w / 1080.0
            g = (ndimage.gaussian_filter(self.out_glow, 2.0 * k) * 1.1
                 + ndimage.gaussian_filter(self.out_glow, 8.0 * k) * 1.4)
            rgb = rgb + (g * cfg.BLOOM)[..., None] * self.sc.flash_rgb
        rgb = rgb * self.mask[..., None]
        idx = np.clip(rgb * 4095.0 + 0.5, 0, 4095).astype(np.uint16)
        return self.lut[idx]


# ----------------------------------------------------------------------------
# frames
# ----------------------------------------------------------------------------
def overlay_state(sc: Scene, tau: float) -> dict:
    tl = sc.timeline
    pos = tl.year_pos(tau)
    n_born = int(np.searchsorted(sc.tau, tau, side="right"))
    return dict(
        year=int(min(max(math.floor(pos), tl.year_base), tl.year_end)),
        year_pos=float(min(pos, tl.year_end + 1.0)),
        count=min(n_born, sc.n_dated),
        stock_tag=float(np.clip((tl.t_years + 0.25 - tau) / 0.45, 0, 1)),
        undated=max(0, n_born - sc.n_dated),
        alpha=float(np.clip(tau / 0.4, 0, 1)),
    )


def make(width, height, ss):
    sc = Scene()
    return sc, Renderer(sc, width, height, ss), Overlay(sc.per_year, sc.n_stock, sc.n_undated,
                                                         width, height)


def frame(sc, rd, ov, tau):
    return composite(rd.render(tau), ov.frame(**overlay_state(sc, tau)))


def _worker(job):
    width, height, ss, fps, frames, out_dir = job
    sc, rd, ov = make(width, height, ss)
    for f in frames:
        img = frame(sc, rd, ov, f / fps)
        Image.fromarray(img).save(out_dir / f"f_{f:05d}.png", compress_level=1)
    return len(frames)


def encode(frames_dir: Path, out: Path, fps: int, crf: int):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", str(frames_dir / "f_%05d.png"),
           "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-tune", "animation",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--still", type=float, help="render one frame at this time (seconds)")
    ap.add_argument("--year", type=float, help="render one frame at the end of this year")
    ap.add_argument("--preview", action="store_true", help="half resolution, 15 fps, 2x supersampling")
    ap.add_argument("--jobs", type=int, default=min(4, mp.cpu_count()))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--keep-frames", action="store_true")
    args = ap.parse_args()

    width, height, ss, fps = cfg.WIDTH, cfg.HEIGHT, cfg.SUPERSAMPLE, cfg.FPS
    if args.preview:
        width, height, ss, fps = width // 2, height // 2, 2, 15
    cfg.OUT.mkdir(exist_ok=True)

    if args.still is not None or args.year is not None:
        sc, rd, ov = make(width, height, ss)
        tau = args.still if args.still is not None else sc.timeline.tau_of(args.year + 1) - 1e-3
        t0 = time.time()
        img = frame(sc, rd, ov, tau)
        out = args.out or cfg.OUT / f"still_{tau:06.2f}s.png"
        Image.fromarray(img).save(out)
        print(f"{out}  (t = {tau:.2f}s, year {sc.timeline.year_pos(tau):.2f}, {time.time() - t0:.1f}s)")
        return

    sc = Scene()
    n_frames = int(round(sc.timeline.t_end * fps))
    frames_dir = cfg.OUT / ("frames_preview" if args.preview else "frames")
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    jobs = max(1, args.jobs)
    chunks = [list(c) for c in np.array_split(np.arange(n_frames), jobs)]
    print(f"{n_frames} frames ({sc.timeline.t_end:.1f}s @ {fps} fps, {width}x{height}, "
          f"{ss}x supersampling) on {jobs} processes")
    # compile the kernels once in the parent so the workers find them cached
    _, rd, ov = make(width, height, ss)
    rd.render(0.0)
    del rd
    t0 = time.time()
    with mp.get_context("fork").Pool(jobs) as pool:
        done = 0
        for k in pool.imap_unordered(
                _worker, [(width, height, ss, fps, c, frames_dir) for c in chunks]):
            done += k
            print(f"  {done}/{n_frames} frames  ({time.time() - t0:.0f}s)", flush=True)
    out = args.out or cfg.OUT / ("la_paz_el_alto_growth_preview.mp4" if args.preview
                                 else "la_paz_el_alto_growth.mp4")
    encode(frames_dir, out, fps, cfg.CRF)
    last = frames_dir / f"f_{n_frames - 1:05d}.png"
    if not args.preview:
        shutil.copy(last, cfg.OUT / "poster.png")
    if not args.keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
    print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
