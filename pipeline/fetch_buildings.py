"""Download Overture Maps building footprints for the La Paz / El Alto AOI.

Overture publishes its buildings theme as ~500 GeoParquet files (~280 GB) in a
public S3 bucket. The files are spatially sorted and every row group carries
min/max statistics for the ``bbox`` struct, so we only need to

1. read each file's footer (two small HTTP range requests),
2. keep the row groups whose bbox statistics touch the AOI, and
3. download just those row groups, filtering rows by their own bbox.

Nothing here needs AWS credentials, DuckDB extensions or the Overture CLI -
plain HTTPS range requests are enough.

Usage:  python -m pipeline.fetch_buildings
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import fsspec
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

from . import config as C

COLUMNS = [
    "id",
    "geometry",
    "bbox",
    "height",
    "num_floors",
    "subtype",
    "class",
    "has_parts",
    "sources",
]
OUT = C.RAW / "overture_buildings.parquet"
INDEX = C.RAW / f"overture_buildings_index_{C.OVERTURE_RELEASE}.json"


def list_files(theme: str = "buildings", type_: str = "building") -> list[str]:
    """Return the HTTPS URLs of all parquet parts of an Overture theme/type."""
    prefix = f"release/{C.OVERTURE_RELEASE}/theme={theme}/type={type_}/"
    urls, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = requests.get(C.OVERTURE_BUCKET_LIST, params=params, timeout=60)
        r.raise_for_status()
        keys = re.findall(r"<Key>([^<]+\.parquet)</Key>", r.text)
        urls += [C.OVERTURE_BUCKET_LIST + k for k in keys]
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", r.text)
        if not m:
            return urls
        token = m.group(1)


def _fs():
    # aiohttp only honours HTTPS_PROXY / SSL_CERT_FILE with trust_env=True.
    return fsspec.filesystem("https", client_kwargs={"trust_env": True})


def _bbox_columns(md: pq.FileMetaData) -> dict[str, int]:
    names = [md.schema.column(i).path for i in range(md.num_columns)]
    return {k: names.index(f"bbox.{k}") for k in ("xmin", "xmax", "ymin", "ymax")}


def row_groups_touching(url: str, aoi: tuple[float, float, float, float]):
    """Return (url, [row group indices], file bbox) using footer statistics only."""
    w, s, e, n = aoi
    for attempt in range(4):
        try:
            with _fs().open(url, "rb", block_size=4 * 2**20, cache_type="bytes") as f:
                md = pq.ParquetFile(f).metadata
            break
        except Exception:  # transient network hiccups
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    cols = _bbox_columns(md)
    hits, fb = [], [180.0, 90.0, -180.0, -90.0]
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        st = {k: rg.column(c).statistics for k, c in cols.items()}
        if any(v is None or not v.has_min_max for v in st.values()):
            hits.append(i)  # no statistics: must read to be safe
            continue
        xmin, xmax = st["xmin"].min, st["xmax"].max
        ymin, ymax = st["ymin"].min, st["ymax"].max
        fb = [min(fb[0], xmin), min(fb[1], ymin), max(fb[2], xmax), max(fb[3], ymax)]
        if xmin <= e and xmax >= w and ymin <= n and ymax >= s:
            hits.append(i)
    return url, hits, fb


def find_row_groups(urls: list[str], aoi) -> dict[str, list[int]]:
    """Scan all footers in parallel; cache the result next to the raw data."""
    if INDEX.exists():
        cached = json.loads(INDEX.read_text())
        if cached.get("aoi") == list(aoi):
            return cached["row_groups"]
    out: dict[str, list[int]] = {}
    t0 = time.time()
    with ThreadPoolExecutor(12) as ex:
        futs = [ex.submit(row_groups_touching, u, aoi) for u in urls]
        for k, fut in enumerate(as_completed(futs), 1):
            url, hits, _ = fut.result()
            if hits:
                out[url] = hits
            if k % 64 == 0 or k == len(futs):
                print(f"  footers {k}/{len(futs)}  ({time.time() - t0:.0f}s, "
                      f"{len(out)} files touch the AOI)", flush=True)
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"aoi": list(aoi), "row_groups": out}, indent=1))
    return out


def read_aoi(url: str, row_groups: list[int], aoi, columns=COLUMNS) -> pa.Table:
    w, s, e, n = aoi
    with _fs().open(url, "rb", block_size=16 * 2**20, cache_type="bytes") as f:
        pf = pq.ParquetFile(f, pre_buffer=True)
        have = set(pf.schema_arrow.names)
        tbl = pf.read_row_groups(row_groups, columns=[c for c in columns if c in have])
    bb = tbl["bbox"].combine_chunks()
    mask = pc.and_(
        pc.and_(pc.less_equal(bb.field("xmin"), e), pc.greater_equal(bb.field("xmax"), w)),
        pc.and_(pc.less_equal(bb.field("ymin"), n), pc.greater_equal(bb.field("ymax"), s)),
    )
    return tbl.filter(mask)


def fetch(theme="buildings", type_="building", aoi=C.AOI_BUILDINGS, out=OUT,
          columns=COLUMNS) -> pa.Table:
    urls = list_files(theme, type_)
    print(f"Overture {C.OVERTURE_RELEASE} {theme}/{type_}: {len(urls)} parquet files")
    groups = find_row_groups(urls, aoi) if (theme, type_) == ("buildings", "building") \
        else {u: h for u, h, _ in (row_groups_touching(u, aoi) for u in urls) if h}
    tables = []
    for url, rgs in groups.items():
        t0 = time.time()
        tbl = read_aoi(url, rgs, aoi, columns)
        print(f"  {url.rsplit('/', 1)[1][:16]}  row groups {rgs}: "
              f"{tbl.num_rows:,} features in AOI ({time.time() - t0:.0f}s)", flush=True)
        if tbl.num_rows:
            tables.append(tbl)
    table = pa.concat_tables(tables, promote_options="default")
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out, compression="zstd")
    print(f"wrote {out}  ({table.num_rows:,} rows, {out.stat().st_size / 1e6:.1f} MB)")
    return table


def main():
    fetch()


if __name__ == "__main__":
    main()
