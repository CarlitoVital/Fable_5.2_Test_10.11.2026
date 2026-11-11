"""Checks for the two pieces of logic everything else rests on:
the dating rules and the rasteriser."""
import numpy as np

from pipeline import config as C
from pipeline import date_buildings as D
from render import raster as R
from render.animate import Timeline
from render.overlay import Ramp


# ----------------------------------------------------------------------------
# dating rules
# ----------------------------------------------------------------------------
def test_neighbourhood_year_ignores_salt_and_pepper():
    """An old district whose raster is sprinkled with late pixels stays old."""
    wsf = np.full((21, 21), 1985, np.int32)
    wsf[::2, ::2] = 2005                       # a quarter of the pixels are noise
    own_is_noise = wsf[10, 10] == 2005
    assert own_is_noise
    assert D.local_percentile_year(wsf, np.array([10]), np.array([10]))[0] == 1985


def test_neighbourhood_year_keeps_a_new_district_new():
    """One stray early pixel must not drag a 2004 district back to 1985."""
    wsf = np.full((21, 21), 2004, np.int32)
    wsf[10, 11] = 1985
    assert D.local_percentile_year(wsf, np.array([10]), np.array([10]))[0] == 2004


def test_neighbourhood_year_without_settlement_is_zero():
    wsf = np.zeros((21, 21), np.int32)
    assert D.local_percentile_year(wsf, np.array([10]), np.array([10]))[0] == 0


def test_neighbourhood_year_ignores_unsettled_pixels():
    wsf = np.zeros((21, 21), np.int32)
    wsf[9, 10] = 1999                          # a single settled neighbour
    assert D.local_percentile_year(wsf, np.array([10]), np.array([10]))[0] == 1999


def test_first_appearance():
    years = np.array(C.OB_TEMPORAL_YEARS)
    pres = np.array([
        [0.00, 0.05, 0.10, 0.60, 0.70, 0.60, 0.70, 0.80],   # built in 2019
        [0.60] * 8,                                         # always there
        [0.00] * 8,                                         # never there
        [0.10, 0.50, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10],   # one-year blip
        [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.50],   # shows up in the last year
    ])
    assert D.first_appearance(pres, years).tolist() == [2019, 0, 0, 0, 2023]


def test_rule_hierarchy():
    #                 wsf   wsf+startup  obt   edge   ghsl  none
    local = np.array([1997, 1987,        0,    2001,  0,    0])
    own = np.array([1997,   1987,        0,    0,     0,    0])
    near = np.array([1997,  1987,        0,    2003,  0,    0])
    appear = np.array([2020, 0,          2019, 0,     0,    0])
    there = np.array([True, True,        False, True, True, True])
    age = np.array([5,      1,           0,    6,     7,    1])
    rnd = np.full(6, 0.5)
    year, method, moved = D.assign_years(local, own, near, appear, there, age, rnd)
    assert method.tolist() == [1, 1, 3, 2, 4, 0]
    assert year[0] == 1997                     # WSF wins over a later Open Buildings date
    assert year[1] == C.YEAR_BASE and moved == 1   # 1987 + "built before 1985" -> stock
    assert year[2] == 2019
    assert year[3] == 2001                     # neighbourhood year, not the nearest pixel
    assert 2000 < year[4] <= 2005              # GHS-AGE class 7 = 2000-2005
    assert year[5] == 0                        # "before 1975" by default is not evidence


def test_hash_is_deterministic_and_uniform():
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-2e4, 2e4, 20000), rng.uniform(-2e4, 2e4, 20000)
    h = D.hash01(x, y, 1)
    assert np.array_equal(h, D.hash01(x, y, 1))
    assert h.min() >= 0 and h.max() < 1
    assert abs(h.mean() - 0.5) < 0.02
    assert np.abs(np.histogram(h, 10, (0, 1))[0] / len(h) - 0.1).max() < 0.02


# ----------------------------------------------------------------------------
# rasteriser
# ----------------------------------------------------------------------------
def _buffers(w=100, h=100):
    return (np.zeros((h, w, 3), np.float32), np.zeros((h, w), np.float32),
            np.zeros((h, w), np.float32))


def test_triangle_covers_its_area():
    color, depth, emis = _buffers()
    R._tri(color, depth, emis, 100, 100, 0.0, 0.0, 1.0, 100.0, 0.0, 1.0, 0.0, 100.0, 1.0,
           1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    assert abs((depth > 0).sum() - 5000) <= 100


def test_two_triangles_tile_a_square_without_gaps():
    color, depth, emis = _buffers()
    for tri in ((0.0, 0.0, 100.0, 0.0, 0.0, 100.0), (100.0, 0.0, 0.0, 100.0, 100.0, 100.0)):
        x0, y0, x1, y1, x2, y2 = tri
        R._tri(color, depth, emis, 100, 100, x0, y0, 1.0, x1, y1, 1.0, x2, y2, 1.0,
               1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0)
    assert (depth > 0).all()


def test_nearer_triangle_wins_regardless_of_order():
    for order in ((1.0, 2.0), (2.0, 1.0)):
        color, depth, emis = _buffers()
        for inv_w in order:
            R._tri(color, depth, emis, 100, 100, 0.0, 0.0, inv_w, 100.0, 0.0, inv_w,
                   0.0, 100.0, inv_w, inv_w, 0.0, 0.0, inv_w, 0.0, 0.0, inv_w, 0.0, 0.0, 0.0)
        assert color[10, 10, 0] == 2.0         # 1/w = 2 is nearer than 1/w = 1


def _one_building(grow, height=30.0):
    W = H = 200
    color, depth, emis = _buffers(W, H)
    ring = np.array([[-20, -20], [20, -20], [20, 20], [-20, 20]], np.float32)  # CCW
    M, _ = R.orbit_camera((0.0, 0.0, 0.0), 20.0, 50.0, 400.0, 30.0, 1.0)
    R.draw_buildings(color, depth, emis, np.ascontiguousarray(M), W, H,
                     ring, np.array([0, 4]), np.array([[0, 1, 2], [0, 2, 3]], np.int32),
                     np.array([0, 2]), np.zeros(1, np.float32), np.full(1, height, np.float32),
                     0, 1, np.full(1, grow, np.float32), np.ones((1, 3), np.float32),
                     np.zeros(1, np.float32), 0.0, 1.0, 1.0, 0.0, 1.0,
                     0.7, 0.7, 0.5, 0.5, 0.5, 1.2, 0.0)
    return color, depth


def test_building_is_drawn_lit_and_grows():
    _, hidden = _one_building(0.0)
    assert (hidden > 0).sum() == 0             # grow = 0: not there yet
    color_half, half = _one_building(0.5)
    color_full, full = _one_building(1.0)
    assert 0 < (half > 0).sum() < (full > 0).sum()
    lit = color_full[full > 0]
    assert lit.max() > 1.0                     # the roof (gain 1.2)
    assert lit.min() < 0.6                     # a wall foot in the shade


def test_building_behind_the_camera_is_skipped():
    W = H = 64
    color, depth, emis = _buffers(W, H)
    ring = np.array([[-5, -5], [5, -5], [5, 5], [-5, 5]], np.float32)
    M, eye = R.orbit_camera((0.0, 0.0, 0.0), 0.0, 45.0, 100.0, 30.0, 1.0)
    ring_far = (ring + eye[:2] * 3).astype(np.float32)      # well behind the eye
    R.draw_buildings(color, depth, emis, np.ascontiguousarray(M), W, H,
                     ring_far, np.array([0, 4]), np.array([[0, 1, 2], [0, 2, 3]], np.int32),
                     np.array([0, 2]), np.zeros(1, np.float32), np.full(1, 10.0, np.float32),
                     0, 1, np.ones(1, np.float32), np.ones((1, 3), np.float32),
                     np.zeros(1, np.float32), 0.0, 1.0, 1.0, 0.0, 1.0,
                     0.7, 0.7, 0.5, 0.5, 0.5, 1.2, 0.0)
    assert (depth > 0).sum() == 0


# ----------------------------------------------------------------------------
# timeline and colour
# ----------------------------------------------------------------------------
def test_timeline_roundtrip_and_order():
    tl = Timeline(1985, 2023)
    for y in (1986.0, 1999.5, 2023.99):
        assert abs(tl.year_pos(tl.tau_of(y)) - y) < 1e-9
    assert 0 < tl.t_stock < tl.t_years < tl.t_reveal < tl.t_end
    assert tl.year_pos(tl.t_years) == 1986.0


def test_ramp_gets_lighter_with_time():
    ramp = Ramp()
    lum = [float(ramp.linear(y) @ [0.2126, 0.7152, 0.0722]) for y in (1985, 1995, 2005, 2015, 2023)]
    assert lum == sorted(lum)
