"""Everything that defines how the animation *looks* - edit freely.

Data questions (area, sources, dating rules) live in ``pipeline/``.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENE = ROOT / "data" / "scene"
FONTS = ROOT / "assets" / "fonts"
OUT = ROOT / "out"

# --- output -----------------------------------------------------------------
WIDTH, HEIGHT = 1080, 1350        # 4:5 portrait (Instagram feed); 1080 x 1920 gives 9:16
FPS = 30
SUPERSAMPLE = 4                   # render at 4x and average: clean edges, no shimmer
CRF = 17                          # x264 quality (lower = better, bigger)

# --- timeline (seconds) -----------------------------------------------------
T_OPEN = 0.5                      # empty landscape, title only
T_STOCK = 3.0                     # the city of 1985 rises, centre outwards
T_STOCK_HOLD = 0.8
T_PER_YEAR = 0.52                 # 1986 ... 2023
T_REVEAL = 1.6                    # undated buildings appear in grey
T_HOLD = 3.4                      # final hold
RISE = 0.45                       # seconds a building takes to grow
FLASH = 0.45                      # e-folding time of the "new building" glow
LIFE = 3.5                        # after this a building is static (baked)

# --- camera -----------------------------------------------------------------
# azimuth 0 = north-up (camera south of the city, looking north);
# elevation = degrees above the horizon; distance in metres; fov vertical.
# The shot eases from CAMERA_START to CAMERA_END over the whole clip. Make the
# two identical for a locked-off camera (renders ~3x faster).
CAMERA_START = dict(target=(-1700.0, -8400.0, 3900.0), azimuth=3.0, elevation=48.0,
                    distance=95000.0, fov=22.0)
CAMERA_END = dict(target=(-3400.0, -10400.0, 3900.0), azimuth=-2.0, elevation=45.0,
                  distance=108000.0, fov=22.0)

Z_REF = 3600.0                    # metres a.s.l. that become z = 0
TERRAIN_EXAGGERATION = 1.7
BUILDING_EXAGGERATION = 9.0       # buildings are tiny at 30 km across - let them stand
MIN_BUILDING_PX = 1.6             # footprints are widened to at least this (supersampled px)
SKIRT = 8.0                       # walls reach this far below ground (hides gaps on slopes)

# --- light & terrain shading ------------------------------------------------
LIGHT_AZIMUTH_FROM_CAMERA = -50.0  # degrees: light comes from the upper left of the frame
LIGHT_ELEVATION = 38.0
TERRAIN_AMBIENT = 0.30
TERRAIN_DIFFUSE = 1.35
TERRAIN_GAIN = 0.74               # overall terrain brightness
TERRAIN_OCCLUSION = 0.55          # darken valleys / brighten ridges (0 = off)
BACKGROUND = "#06090d"
FOG_COLOR = "#0a1118"
FOG_START = 105000.0              # metres from the camera
FOG_LENGTH = 60000.0
FOG_STRENGTH = 0.85
VIGNETTE = 0.38

WALL_AMBIENT, WALL_DIFFUSE = 0.50, 0.62
WALL_FOOT = 0.50                  # wall brightness at ground level relative to the eaves
ROOF_GAIN = 1.12

# --- colour -----------------------------------------------------------------
# Building colour by year: deep crimson (1985 stock) -> red -> orange -> pale peach.
RAMP = [
    (1985.0, "#701428"),
    (1986.0, "#b8102a"),
    (1994.0, "#e02a2a"),
    (2003.0, "#f2602f"),
    (2012.0, "#f9a35c"),
    (2023.99, "#fde6c8"),
]
FLASH_COLOR = "#fff4dc"
FLASH_MIX = 0.9                   # how far a brand-new building is pushed towards FLASH_COLOR
UNDATED_COLOR = "#59616b"
BLOOM = 0.8                       # glow strength of new construction (0 = off)

# --- words ------------------------------------------------------------------
KICKER = "BOLIVIA  ·  1985 – 2023"
TITLE = "LA PAZ · EL ALTO"
CHART_LABEL = "BUILDINGS ADDED PER YEAR"
STOCK_LABEL = "AND EARLIER"
NOTE = ("Year = first satellite detection of a settlement at the building's site, not a "
        "cadastral date (Landsat 1985–2015, Sentinel-2 2016–23)")
AUTHOR = ""                       # e.g. "Author: Your Name" - shown bottom left when set
CREDITS = ("Dark red = standing by 1985 · grey = undated · Data: Overture Maps, DLR WSF Evolution, "
           "Google Open Buildings 2.5D, JRC GHSL, Copernicus DEM, ESA WorldCover")

# Place labels: (text, lon, lat, offset of the text from its anchor in px at 1080 wide).
# "{z}" is replaced by the terrain elevation at the anchor, rounded to 10 m.
LABELS = [
    ("EL ALTO · {z} m", -68.1940, -16.4870, (-70, -96)),
    ("LA PAZ · {z} m", -68.1336, -16.4955, (36, -128)),
    ("ZONA SUR · {z} m", -68.0850, -16.5400, (60, -84)),
]

TEXT_COLOR = "#e9e6e1"
TEXT_DIM = "#9aa3ab"
ACCENT = "#ef4a35"
