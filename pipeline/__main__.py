"""Run the data pipeline end to end, or only the named steps.

    python -m pipeline                    everything, in order
    python -m pipeline date scene         just re-date the buildings and rebuild the scene

Steps:  buildings  rasters  sample  date  scene
"""
from __future__ import annotations

import sys
import time

from . import build_scene, date_buildings, fetch_buildings, fetch_rasters, sample

STEPS = {
    "buildings": fetch_buildings.main,   # Overture footprints            -> data/raw
    "rasters": fetch_rasters.main,       # WSF, GHSL, Open Buildings, DEM  -> data/raw
    "sample": sample.main,               # evidence per building          -> data/processed
    "date": date_buildings.main,         # year + height per building     -> data/processed
    "scene": build_scene.main,           # arrays for the renderer        -> data/scene
}

if __name__ == "__main__":
    todo = sys.argv[1:] or list(STEPS)
    unknown = [s for s in todo if s not in STEPS]
    if unknown:
        sys.exit(f"unknown step(s) {unknown}; choose from {list(STEPS)}")
    for name in todo:
        t0 = time.time()
        print(f"\n=== {name} ===", flush=True)
        STEPS[name]()
        print(f"--- {name} done in {time.time() - t0:.0f}s", flush=True)
