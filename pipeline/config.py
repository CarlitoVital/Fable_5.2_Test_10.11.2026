"""Shared configuration for the La Paz / El Alto urban-growth pipeline.

Everything that defines *what* is mapped lives here: the area of interest,
the remote data sources and the local file layout. Rendering / styling
parameters live in ``web/config.js`` instead.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
SCENE = ROOT / "web" / "scene"

# --------------------------------------------------------------------------
# Area of interest (WGS84 lon/lat: west, south, east, north)
# --------------------------------------------------------------------------
# Buildings are fetched for the whole La Paz - El Alto conurbation (Viacha in
# the south-west and Mecapaca down-valley are deliberately left out).
AOI_BUILDINGS = (-68.335, -16.675, -68.000, -16.405)

# Left out on purpose: Viacha, a separate town 20 km south-west of El Alto. It
# sits in the corner of the AOI and would only ever appear cut off at the edge
# of the frame.
EXCLUDE_BOXES = [(-68.335, -16.675, -68.270, -16.632)]

# Terrain / land cover backdrop: much larger, so the tilted camera never sees
# the edge of the world.
AOI_TERRAIN = (-68.62, -16.95, -67.70, -16.12)

# Local metric CRS for all geometry work: WGS 84 / UTM zone 19S.
CRS_METRIC = "EPSG:32719"

# Scene origin (Plaza Murillo, the historic centre) - coordinates handed to the
# renderer are metres east/north of this point.
ORIGIN_LONLAT = (-68.1336, -16.4955)

# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------
# WSF Evolution starts with the 1985 Landsat record: whatever it sees then is
# "1985 or earlier" - the stock the animation opens with. (GHSL has a 1975
# epoch too, but its 1980 and 1985 layers are interpolations, and its 1975
# layer shows only about half of the pre-war centre as built - see README.)
YEAR_BASE = 1985
# Last year with direct evidence (Google Open Buildings 2.5D Temporal).
YEAR_END = 2023

GHSL_EPOCHS = [1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020]

# --------------------------------------------------------------------------
# Remote sources (all open data, all fetched over plain HTTPS range requests)
# --------------------------------------------------------------------------
OVERTURE_RELEASE = "2026-10-21.0"
OVERTURE_BASE = (
    "https://overturemaps-us-west-2.s3.amazonaws.com/release/" + OVERTURE_RELEASE
)
OVERTURE_BUCKET_LIST = "https://overturemaps-us-west-2.s3.amazonaws.com/"

GHSL = "https://jrc-ghsl.s3.amazonaws.com"
GHSL_BUILT_S = (
    GHSL + "/ghs-built-s/r2023a/54009/100m/{year}/"
    "GHS_BUILT_S_E{year}_GLOBE_R2023A_54009_100_V1_0.tif"
)
GHSL_AGE = (
    GHSL + "/ghs-age/r2025a/54009/100m/1975052020/"
    "GHS_AGE_1975052020_GLOBE_R2025A_54009_100_V1_0.tif"
)
GHSL_ANBH = (
    GHSL + "/ghs-built-h-anbh/r2023a/54009/100m/2018/"
    "GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_54009_100_V1_0.tif"
)

# DLR World Settlement Footprint Evolution: 2x2 degree tiles named after their
# lower-left corner. La Paz / El Alto sits in the (-70, -18) tile.
WSF_EVO = (
    "https://download.geoservice.dlr.de/WSF_EVO/files/"
    "WSFevolution_v1_{lon}_{lat}/WSFevolution_v1_{lon}_{lat}.tif"
)
WSF_EVO_TILES = [(-70, -18), (-68, -18)]

# Copernicus DEM GLO-30 (1x1 degree COGs, named after the south-west corner).
COP_DEM = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)

# ESA WorldCover 2021 v200 (3x3 degree COGs, named after the south-west corner).
WORLDCOVER = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{ns}{lat:02d}{ew}{lon:03d}_Map.tif"
)

# Google Open Buildings 2.5D Temporal (annual 2016-2023, 4 m, per UTM zone).
OB_TEMPORAL_API = (
    "https://storage.googleapis.com/storage/v1/b/open-buildings-temporal-data/o"
)
OB_TEMPORAL_HTTP = "https://storage.googleapis.com/open-buildings-temporal-data/"
OB_TEMPORAL_YEARS = list(range(2016, 2024))


def gdal_env() -> dict:
    """GDAL settings for fast, polite HTTP range reads of cloud-optimised TIFFs."""
    return dict(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="2",
        GDAL_HTTP_MULTIPLEX="YES",
        VSI_CACHE="TRUE",
        VSI_CACHE_SIZE=str(64 * 1024 * 1024),
    )
