"""A small software rasteriser (NumPy + Numba), enough for one city.

Why not a GPU? The animation has to render on machines without one (CI
runners, cloud sandboxes), where software OpenGL/WebGL takes ~10 s for a
frame of this scene. A purpose-built z-buffer rasteriser compiled with Numba
draws the same ~8 million triangles in about a second, has no system
dependencies and gives pixel-identical results everywhere.

Conventions
-----------
* world: x east, y north, z up (metres, already vertically exaggerated)
* screen: x right, y down, pixel centres at +0.5
* depth buffer stores 1/w ("bigger is nearer", 0 = empty); 1/w interpolates
  linearly in screen space, so no perspective correction is needed for it
* colours are linear RGB float32; ``emis`` is a per-pixel glow weight
"""
from __future__ import annotations

import math

import numpy as np
from numba import njit


@njit(cache=True, fastmath=True, inline="always")
def _tri(color, depth, emis, W, H,
         x0, y0, w0, x1, y1, w1, x2, y2, w2,
         r0, g0, b0, r1, g1, b1, r2, g2, b2, e):
    """Gouraud triangle with z-test. Vertices: screen x, y and 1/w."""
    xmin = min(x0, x1, x2)
    xmax = max(x0, x1, x2)
    ymin = min(y0, y1, y2)
    ymax = max(y0, y1, y2)
    px0 = int(math.ceil(xmin - 0.5))
    px1 = int(math.floor(xmax - 0.5))
    py0 = int(math.ceil(ymin - 0.5))
    py1 = int(math.floor(ymax - 0.5))
    if px0 < 0:
        px0 = 0
    if py0 < 0:
        py0 = 0
    if px1 > W - 1:
        px1 = W - 1
    if py1 > H - 1:
        py1 = H - 1
    if px0 > px1 or py0 > py1:
        return
    area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(area) < 1e-12:
        return
    inv = 1.0 / area
    for py in range(py0, py1 + 1):
        cy = py + 0.5
        for px in range(px0, px1 + 1):
            cx = px + 0.5
            a = ((x1 - cx) * (y2 - cy) - (x2 - cx) * (y1 - cy)) * inv
            b = ((x2 - cx) * (y0 - cy) - (x0 - cx) * (y2 - cy)) * inv
            c = 1.0 - a - b
            if a >= 0.0 and b >= 0.0 and c >= 0.0:
                w = a * w0 + b * w1 + c * w2
                if w > depth[py, px]:
                    depth[py, px] = w
                    color[py, px, 0] = a * r0 + b * r1 + c * r2
                    color[py, px, 1] = a * g0 + b * g1 + c * g2
                    color[py, px, 2] = a * b0 + b * b1 + c * b2
                    emis[py, px] = e


@njit(cache=True, fastmath=True)
def draw_grid(color, depth, emis, M, W, H, gx0, gy1, res, Z, RGB,
              zref, zscale, near, hole):
    """Draw a regular height grid (vertices Z, per-vertex colours RGB).

    ``hole`` = (i0, j0, ni, nj): cells of that block are skipped (it is where a
    finer grid takes over); pass ni = 0 for none.
    """
    ny, nx = Z.shape
    SX = np.empty((ny, nx), np.float64)
    SY = np.empty((ny, nx), np.float64)
    SW = np.empty((ny, nx), np.float64)
    for j in range(ny):
        y = gy1 - j * res
        for i in range(nx):
            x = gx0 + i * res
            z = (Z[j, i] - zref) * zscale
            cw = M[3, 0] * x + M[3, 1] * y + M[3, 2] * z + M[3, 3]
            if cw <= near:
                SW[j, i] = -1.0
                continue
            iw = 1.0 / cw
            SX[j, i] = ((M[0, 0] * x + M[0, 1] * y + M[0, 2] * z + M[0, 3]) * iw * 0.5 + 0.5) * W
            SY[j, i] = (0.5 - (M[1, 0] * x + M[1, 1] * y + M[1, 2] * z + M[1, 3]) * iw * 0.5) * H
            SW[j, i] = iw
    hi0, hj0, hni, hnj = hole
    for j in range(ny - 1):
        for i in range(nx - 1):
            if hni > 0 and i >= hi0 and i < hi0 + hni and j >= hj0 and j < hj0 + hnj:
                continue
            wa = SW[j, i]
            wb = SW[j + 1, i]
            wc = SW[j, i + 1]
            wd = SW[j + 1, i + 1]
            if wa <= 0.0 or wb <= 0.0 or wc <= 0.0 or wd <= 0.0:
                continue
            xa = SX[j, i]; xb = SX[j + 1, i]; xc = SX[j, i + 1]; xd = SX[j + 1, i + 1]
            if (xa < 0 and xb < 0 and xc < 0 and xd < 0) or \
               (xa > W and xb > W and xc > W and xd > W):
                continue
            ya = SY[j, i]; yb = SY[j + 1, i]; yc = SY[j, i + 1]; yd = SY[j + 1, i + 1]
            if (ya < 0 and yb < 0 and yc < 0 and yd < 0) or \
               (ya > H and yb > H and yc > H and yd > H):
                continue
            _tri(color, depth, emis, W, H, xa, ya, wa, xb, yb, wb, xc, yc, wc,
                 RGB[j, i, 0], RGB[j, i, 1], RGB[j, i, 2],
                 RGB[j + 1, i, 0], RGB[j + 1, i, 1], RGB[j + 1, i, 2],
                 RGB[j, i + 1, 0], RGB[j, i + 1, 1], RGB[j, i + 1, 2], 0.0)
            _tri(color, depth, emis, W, H, xc, yc, wc, xb, yb, wb, xd, yd, wd,
                 RGB[j, i + 1, 0], RGB[j, i + 1, 1], RGB[j, i + 1, 2],
                 RGB[j + 1, i, 0], RGB[j + 1, i, 1], RGB[j + 1, i, 2],
                 RGB[j + 1, i + 1, 0], RGB[j + 1, i + 1, 1], RGB[j + 1, i + 1, 2], 0.0)


@njit(cache=True, fastmath=True)
def draw_buildings(color, depth, emis, M, W, H,
                   ring_xy, ring_off, roof_idx, roof_off, ground, height,
                   b0, b1, grow, rgb, glow,
                   zref, zscale, hscale, skirt, near,
                   lx, ly, wall_amb, wall_dif, wall_foot, roof_gain, min_px):
    """Extrude and draw buildings b0..b1-1.

    Per building (arrays indexed by building): ``grow`` 0..1 scales the height
    (0 = not drawn), ``rgb`` is its colour and ``glow`` its emissive weight.
    Walls are lit by a horizontal light (lx, ly), darkened towards the foot
    (cheap ambient occlusion); roofs get ``roof_gain``. ``min_px`` widens
    footprints that would project smaller than that many pixels, so that small
    houses do not flicker in and out of the sampling grid.
    """
    nmax = 0
    for b in range(b0, b1):
        n = ring_off[b + 1] - ring_off[b]
        if n > nmax:
            nmax = n
    bx = np.empty(nmax, np.float64); by = np.empty(nmax, np.float64); bw = np.empty(nmax, np.float64)
    tx = np.empty(nmax, np.float64); ty = np.empty(nmax, np.float64); tw = np.empty(nmax, np.float64)
    px = np.empty(nmax, np.float64); py = np.empty(nmax, np.float64)
    drawn = 0
    for b in range(b0, b1):
        g = grow[b]
        if g <= 0.0:
            continue
        s = ring_off[b]
        n = ring_off[b + 1] - s
        if n < 3:
            continue
        zg = (ground[b] - zref) * zscale
        zb = zg - skirt
        zt = zg + height[b] * hscale * g
        # footprint, optionally inflated about its centroid
        mx = 0.0; my = 0.0
        for k in range(n):
            px[k] = ring_xy[s + k, 0]; py[k] = ring_xy[s + k, 1]
            mx += px[k]; my += py[k]
        mx /= n; my /= n
        if min_px > 0.0:
            # projected size of one metre at the building (pixels)
            cwc = M[3, 0] * mx + M[3, 1] * my + M[3, 2] * zg + M[3, 3]
            if cwc <= near:
                continue
            ppm = 0.5 * W * math.sqrt(M[0, 0] * M[0, 0] + M[0, 1] * M[0, 1]) / cwc
            r2 = 0.0
            for k in range(n):
                d2 = (px[k] - mx) ** 2 + (py[k] - my) ** 2
                if d2 > r2:
                    r2 = d2
            size = 2.0 * math.sqrt(r2) * ppm * 0.7
            if size < min_px and size > 1e-9:
                f = min_px / size
                if f > 3.0:
                    f = 3.0
                for k in range(n):
                    px[k] = mx + (px[k] - mx) * f
                    py[k] = my + (py[k] - my) * f
        ok = True
        sxmin = 1e30; sxmax = -1e30; symin = 1e30; symax = -1e30
        for k in range(n):
            x = px[k]; y = py[k]
            cw = M[3, 0] * x + M[3, 1] * y + M[3, 2] * zb + M[3, 3]
            if cw <= near:
                ok = False
                break
            iw = 1.0 / cw
            bx[k] = ((M[0, 0] * x + M[0, 1] * y + M[0, 2] * zb + M[0, 3]) * iw * 0.5 + 0.5) * W
            by[k] = (0.5 - (M[1, 0] * x + M[1, 1] * y + M[1, 2] * zb + M[1, 3]) * iw * 0.5) * H
            bw[k] = iw
            cw = M[3, 0] * x + M[3, 1] * y + M[3, 2] * zt + M[3, 3]
            if cw <= near:
                ok = False
                break
            iw = 1.0 / cw
            tx[k] = ((M[0, 0] * x + M[0, 1] * y + M[0, 2] * zt + M[0, 3]) * iw * 0.5 + 0.5) * W
            ty[k] = (0.5 - (M[1, 0] * x + M[1, 1] * y + M[1, 2] * zt + M[1, 3]) * iw * 0.5) * H
            tw[k] = iw
            if bx[k] < sxmin: sxmin = bx[k]
            if bx[k] > sxmax: sxmax = bx[k]
            if tx[k] < sxmin: sxmin = tx[k]
            if tx[k] > sxmax: sxmax = tx[k]
            if by[k] < symin: symin = by[k]
            if by[k] > symax: symax = by[k]
            if ty[k] < symin: symin = ty[k]
            if ty[k] > symax: symax = ty[k]
        if not ok or sxmax < 0 or sxmin > W or symax < 0 or symin > H:
            continue
        drawn += 1
        r = rgb[b, 0]; gg = rgb[b, 1]; bl = rgb[b, 2]
        e = glow[b]
        # walls (only those facing the camera)
        for k in range(n):
            k2 = k + 1
            if k2 == n:
                k2 = 0
            ax = bx[k2] - bx[k]; ay = by[k2] - by[k]
            cx = tx[k2] - bx[k]; cy = ty[k2] - by[k]
            if ax * cy - ay * cx >= 0.0:
                continue
            dx = px[k2] - px[k]; dy = py[k2] - py[k]
            L = math.sqrt(dx * dx + dy * dy)
            if L < 1e-6:
                continue
            nd = (dy * lx - dx * ly) / L
            if nd < 0.0:
                nd = 0.0
            sh = wall_amb + wall_dif * nd
            rt = r * sh; gt = gg * sh; bt = bl * sh
            rb = rt * wall_foot; gb = gt * wall_foot; bb = bt * wall_foot
            _tri(color, depth, emis, W, H, bx[k], by[k], bw[k], bx[k2], by[k2], bw[k2],
                 tx[k2], ty[k2], tw[k2], rb, gb, bb, rb, gb, bb, rt, gt, bt, e)
            _tri(color, depth, emis, W, H, bx[k], by[k], bw[k], tx[k2], ty[k2], tw[k2],
                 tx[k], ty[k], tw[k], rb, gb, bb, rt, gt, bt, rt, gt, bt, e)
        # roof
        rr = r * roof_gain; rg = gg * roof_gain; rbl = bl * roof_gain
        for m in range(roof_off[b], roof_off[b + 1]):
            i0 = roof_idx[m, 0]; i1 = roof_idx[m, 1]; i2 = roof_idx[m, 2]
            _tri(color, depth, emis, W, H, tx[i0], ty[i0], tw[i0], tx[i1], ty[i1], tw[i1],
                 tx[i2], ty[i2], tw[i2], rr, rg, rbl, rr, rg, rbl, rr, rg, rbl, e)
    return drawn


# ----------------------------------------------------------------------------
# camera
# ----------------------------------------------------------------------------
def look_at(eye, target, up=(0.0, 0.0, 1.0)):
    eye, target, up = (np.asarray(v, float) for v in (eye, target, up))
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    V = np.eye(4)
    V[0, :3], V[1, :3], V[2, :3] = s, u, -f
    V[:3, 3] = -V[:3, :3] @ eye
    return V


def perspective(fov_y_deg, aspect, near, far):
    t = 1.0 / math.tan(math.radians(fov_y_deg) / 2)
    P = np.zeros((4, 4))
    P[0, 0] = t / aspect
    P[1, 1] = t
    P[2, 2] = (far + near) / (near - far)
    P[2, 3] = 2 * far * near / (near - far)
    P[3, 2] = -1.0
    return P


def orbit_camera(target, azimuth_deg, elevation_deg, distance, fov_y_deg, aspect):
    """Camera looking at ``target`` from compass ``azimuth`` (0 = from the south
    looking north, 90 = from the west looking east), ``elevation`` degrees
    above the horizon, ``distance`` metres away. Returns (M, eye)."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    # direction from target to eye
    d = np.array([-math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])
    eye = np.asarray(target, float) + d * distance
    V = look_at(eye, target)
    P = perspective(fov_y_deg, aspect, distance * 0.05, distance * 30)
    return P @ V, eye
