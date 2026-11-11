"""2D layer on top of the 3D frame: title, year counter, histogram, credits."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config as cfg


def hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, dtype=np.float64), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


class Ramp:
    """Year -> colour, interpolated in linear light."""

    def __init__(self, stops=cfg.RAMP):
        self.years = np.array([s[0] for s in stops], dtype=np.float64)
        self.lin = srgb_to_linear(np.array([hex_rgb(s[1]) for s in stops]) / 255.0)

    def linear(self, year):
        year = np.asarray(year, dtype=np.float64)
        return np.stack([np.interp(year, self.years, self.lin[:, k]) for k in range(3)], -1)

    def srgb8(self, year):
        return tuple(int(round(v * 255)) for v in linear_to_srgb(self.linear(year)))


def font(name: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(cfg.FONTS / f"Montserrat-{name}.ttf"), size)


def tracked(draw, xy, text, fnt, fill, tracking=0.0, anchor="ls"):
    """Draw text with letter-spacing; returns the x where it ends."""
    x, y = xy
    if anchor[0] == "r":
        x -= tracked_width(text, fnt, tracking)
        anchor = "l" + anchor[1]
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill, anchor=anchor)
        x += fnt.getlength(ch) + tracking
    return x - tracking


def tracked_width(text, fnt, tracking=0.0):
    return sum(fnt.getlength(ch) for ch in text) + tracking * max(len(text) - 1, 0)


def fit(text, name, size, max_width, min_size=10.0):
    """Largest font size <= ``size`` at which ``text`` fits ``max_width``."""
    while size > min_size:
        f = font(name, size)
        if f.getlength(text) <= max_width:
            return f
        size -= 0.25
    return font(name, min_size)


class Overlay:
    def __init__(self, per_year: dict[int, int], n_stock: int, n_undated: int,
                 width=cfg.WIDTH, height=cfg.HEIGHT):
        self.W, self.H = width, height
        self.k = width / 1080.0                      # layout scale
        self.per_year = per_year
        self.years = sorted(y for y in per_year if y > min(per_year))
        self.y0, self.y1 = self.years[0], self.years[-1]
        self.n_stock, self.n_undated = n_stock, n_undated
        self.ramp = Ramp()
        k = self.k
        self.mx = round(64 * k)                      # left margin
        # The layout was drawn for 1080 x 1350. The title block hangs from the top,
        # everything else stands on the bottom edge, so taller frames (9:16) just
        # get more landscape in between.
        self.by = lambda y: height - (1350 - y) * k
        # chart geometry
        self.bar_w, self.bar_gap = 14 * k, 5 * k
        self.chart_x = self.mx + 2 * k
        self.chart_base = round(self.by(1236))
        self.chart_h = 112 * k
        self.vmax = max(per_year[y] for y in self.years)
        self.f_year = font("ExtraBold", 66 * k)
        self.f_count = font("SemiBold", 19 * k)
        self.f_label = font("SemiBold", 14.5 * k)
        self.f_tick = font("Medium", 13.5 * k)
        self.f_tag = font("SemiBold", 15 * k)
        self.static = self._static()

    # ------------------------------------------------------------------ static
    def _static(self) -> Image.Image:
        k, W, H = self.k, self.W, self.H
        text = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(text)
        col, dim, acc = hex_rgb(cfg.TEXT_COLOR), hex_rgb(cfg.TEXT_DIM), hex_rgb(cfg.ACCENT)
        # title block
        tracked(d, (self.mx + 2 * k, 92 * k), cfg.KICKER, font("SemiBold", 20 * k), acc + (255,),
                tracking=5.5 * k)
        f_title = fit(cfg.TITLE, "ExtraBold", 70 * k, W - 2 * self.mx)
        d.text((self.mx - 2 * k, 166 * k), cfg.TITLE, font=f_title, fill=col + (255,), anchor="ls")
        # chart furniture
        tracked(d, (self.mx + 2 * k, self.by(1106)), cfg.CHART_LABEL, self.f_label, col + (215,),
                tracking=2.6 * k)
        x_end = self.x_of(self.y1 + 1) + 70 * k
        d.line([(self.mx, self.chart_base + 1), (x_end, self.chart_base + 1)],
               fill=dim + (110,), width=max(1, round(k)))
        for y in self.years:
            if y % 10 == 0:
                d.text((self.x_of(y) + self.bar_w / 2, self.chart_base + 20 * k), str(y),
                       font=self.f_tick, fill=dim + (230,), anchor="ms")
        # notes
        avail = W - 2 * self.mx
        f_note = fit(cfg.NOTE, "Medium", 13 * k, avail, 9.5 * k)
        d.text((self.mx, self.by(1285)), cfg.NOTE, font=f_note, fill=dim + (215,), anchor="ls")
        if cfg.AUTHOR:
            f_auth = font("Medium", 13 * k)
            d.text((self.mx, self.by(1310)), cfg.AUTHOR, font=f_auth, fill=dim + (235,), anchor="ls")
            room = avail - f_auth.getlength(cfg.AUTHOR) - 28 * k
            f_cred = fit(cfg.CREDITS, "Medium", 13 * k, room, 9.0 * k)
            d.text((W - self.mx, self.by(1310)), cfg.CREDITS, font=f_cred, fill=dim + (215,),
                   anchor="rs")
        else:
            f_cred = fit(cfg.CREDITS, "Medium", 13 * k, avail, 9.5 * k)
            d.text((self.mx, self.by(1310)), cfg.CREDITS, font=f_cred, fill=dim + (215,), anchor="ls")
        # soft dark halo behind all lettering keeps it readable over the terrain
        alpha = text.getchannel("A")
        halo = alpha.filter(ImageFilter.GaussianBlur(9 * k)).point(lambda v: min(255, int(v * 1.5)))
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        out.putalpha(halo.point(lambda v: int(v * 0.55)))
        # faint light bloom on the title, as on a backlit sign
        title_only = Image.new("L", (W, H), 0)
        ImageDraw.Draw(title_only).text((self.mx - 2 * k, 166 * k), cfg.TITLE, font=f_title,
                                        fill=255, anchor="ls")
        glow = title_only.filter(ImageFilter.GaussianBlur(7 * k)).point(lambda v: int(v * 0.30))
        glow_rgba = Image.new("RGBA", (W, H), col + (0,))
        glow_rgba.putalpha(glow)
        out = Image.alpha_composite(out, glow_rgba)
        return Image.alpha_composite(out, text)

    def x_of(self, year: float) -> float:
        return self.chart_x + (year - self.y0) * (self.bar_w + self.bar_gap)

    # ----------------------------------------------------------------- dynamic
    def frame(self, *, year: int, year_pos: float, count: int, stock_tag: float,
              undated: int, alpha: float = 1.0, labels=()) -> np.ndarray:
        """RGBA uint8 overlay for one frame.

        year      label shown in the big counter
        year_pos  continuous timeline position (e.g. 1993.4); bars up to it are drawn
        count     dated buildings standing so far
        stock_tag 0..1 opacity of the "already standing in 1985" tag
        undated   number of undated buildings revealed so far (0 = hide)
        labels    place labels as (text, x, y, dx, dy, opacity): anchor in pixels,
                  text offset from it
        """
        k = self.k
        im = self.static.copy()
        d = ImageDraw.Draw(im)
        col, dim = hex_rgb(cfg.TEXT_COLOR), hex_rgb(cfg.TEXT_DIM)

        # big year, tinted with the colour buildings of that year get
        c = np.array(self.ramp.srgb8(max(year_pos, 1989.0)), dtype=float)
        c = tuple(int(v) for v in np.clip(c * 0.9 + 255 * 0.1, 0, 255))
        ybase = self.by(1050)
        d.text((self.mx - 3 * k, ybase), str(year), font=self.f_year, fill=c + (255,), anchor="ls")
        if stock_tag > 0.01:
            x = self.mx + self.f_year.getlength(str(year)) + 16 * k
            tracked(d, (x, ybase - 3 * k), cfg.STOCK_LABEL, self.f_tag,
                    dim + (int(235 * stock_tag),), tracking=2.2 * k)
        # count line
        x = tracked(d, (self.mx + 2 * k, self.by(1081)), f"{count:,} BUILDINGS", self.f_count,
                    col + (235,), tracking=2.0 * k)
        if undated > 0:
            tracked(d, (x + 18 * k, self.by(1081)), f"+ {undated:,} UNDATED", self.f_count,
                    hex_rgb("#8b949e") + (235,), tracking=2.0 * k)

        # histogram
        for y in self.years:
            p = min(max(year_pos - y, 0.0), 1.0)
            if p <= 0:
                break
            h = self.chart_h * self.per_year[y] / self.vmax * p
            x0 = self.x_of(y)
            if h >= 0.5:
                d.rectangle([x0, self.chart_base - h, x0 + self.bar_w, self.chart_base],
                            fill=self.ramp.srgb8(y + 0.5) + (255,))
        # playhead
        if year_pos > self.y0:
            xp = min(self.x_of(min(year_pos, self.y1 + 1)), self.x_of(self.y1 + 1) - self.bar_gap)
            top = self.chart_base - self.chart_h - 14 * k
            d.line([(xp, top), (xp, self.chart_base)], fill=col + (200,), width=max(1, round(1.5 * k)))
            s = 5 * k
            d.polygon([(xp - s, top - s * 1.2), (xp + s, top - s * 1.2), (xp, top + s * 0.4)],
                      fill=col + (230,))
        if labels:
            im = Image.alpha_composite(self._labels(labels), im)
        arr = np.asarray(im, dtype=np.uint8)
        if alpha < 0.999:
            arr = arr.copy()
            arr[..., 3] = (arr[..., 3].astype(np.float32) * alpha).astype(np.uint8)
        return arr

    def _labels(self, labels) -> Image.Image:
        """Pins with a hairline leader: small, quiet, there for orientation only."""
        k = self.k
        lay = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        col = hex_rgb(cfg.TEXT_COLOR)
        f = font("SemiBold", 13.5 * k)
        for text, x, y, dx, dy, op in labels:
            if op <= 0.01:
                continue
            tx, ty = x + dx * k, y + dy * k
            a = int(255 * op)
            d.line([(x, y), (tx, ty)], fill=col + (int(a * 0.55),), width=max(1, round(k)))
            r = 2.6 * k
            d.ellipse([x - r, y - r, x + r, y + r], fill=col + (a,))
            w = tracked_width(text, f, 2.0 * k)
            left = tx - w / 2
            pad = 7 * k
            d.rounded_rectangle([left - pad, ty - 22 * k, left + w + pad, ty + 1 * k],
                                radius=4 * k, fill=(8, 12, 16, int(150 * op)))
            tracked(d, (left, ty - 6 * k), text, f, col + (a,), tracking=2.0 * k)
        return lay


def composite(frame_rgb: np.ndarray, overlay_rgba: np.ndarray) -> np.ndarray:
    """Alpha-blend an RGBA uint8 overlay over an RGB uint8 frame."""
    a = overlay_rgba[..., 3:4].astype(np.uint16)
    out = (frame_rgb.astype(np.uint16) * (255 - a) + overlay_rgba[..., :3].astype(np.uint16) * a
           + 127) // 255
    return out.astype(np.uint8)
