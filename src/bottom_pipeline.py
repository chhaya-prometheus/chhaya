"""
Monthly slow pipeline orchestrator.

Reads the H3 hex grid fixture, runs every source, merges columns
with the previous cells.parquet (two-tier: failed sources keep old
values), writes cells.parquet + cells.geojson locally, uploads
everything to S3, and writes pipeline_status.json.

Usage:
    py -m src.pipeline_slow
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd

from config import load_config
from utils.logger import configLogger
from src.export import S3Exporter
from src.sources.ndvi import Sentinel2Source

log = configLogger(__file__)
cfg = load_config()


# Register sources here as they are built.
# Order matters only for logging; merges are keyed on h3_index.
SOURCES = [
    Sentinel2Source,
    # WorldPopSource,
    # OSMSource,
    # LandsatSource,
    # DustSource,
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_source(SourceClass, grid: gpd.GeoDataFrame):
    """
    Run one source.

    Returns:
        (col_df, status_dict)
        col_df  — GeoDataFrame with [h3_index, ...source columns]
        status_dict — {ok, ran_at, nulls, error}
    """
    name = SourceClass.__name__
    log.info(f"--- {name} ---")

    try:
        col_df = SourceClass(cfg, log).run()

        # Count nulls across all source columns
        source_cols = [c for c in col_df.columns if c != "h3_index"]
        nulls = int(col_df[source_cols].isna().any(axis=1).sum())

        status = {
            "ok": True,
            "ran_at": _now_iso(),
            "nulls": nulls,
            "error": None,
        }
        log.info(f"{name} done — columns={source_cols}, nulls={nulls}")
        return col_df, status

    except Exception as e:
        log.error(f"{name} failed: {type(e).__name__}: {e}")
        # Empty dataframe with h3_index only; merge will leave columns null
        empty = gpd.GeoDataFrame(
            {"h3_index": grid["h3_index"].values},
            geometry=grid.geometry,
            crs=grid.crs,
        )
        status = {
            "ok": False,
            "ran_at": None,
            "nulls": None,
            "error": f"{type(e).__name__}: {e}",
        }
        return empty, status


def main() -> int:
    derived_dir = Path(cfg["local"]["derived_dir"])
    derived_dir.mkdir(parents=True, exist_ok=True)

    # 1. load grid fixture
    grid_path = Path(cfg["fixtures"]["local_dir"]) / "delhi_hex_res8.parquet"
    log.info(f"loading grid fixture: {grid_path}")
    grid = gpd.read_parquet(grid_path)
    log.info(f"grid loaded — {len(grid)} hexes")

    # 2. load previous cells.parquet from S3 (None on first run)
    exporter = S3Exporter(cfg, log)
    old_cells = exporter.load_existing_cells()

    # 3. run all sources, track per-source status
    cells = grid[["h3_index", "geometry"]].copy()
    statuses = {}
    ok_columns = set()

    for SourceClass in SOURCES:
        col_df, status = run_source(SourceClass, grid)
        key = SourceClass.__name__.replace("Source", "").lower()  # e.g. "sentinel2" -> "sentinel2"
        statuses[key] = status

        if status["ok"]:
            ok_columns.update(c for c in col_df.columns if c != "h3_index")

        cells = cells.merge(col_df, on="h3_index", how="left")

    # 4. two-tier merge with previous run
    cells = exporter.merge_preserving_old(cells, old_cells, ok_columns)
    log.info(f"final table — {len(cells)} rows, columns={list(cells.columns)}")

    # 5. write locally
    out_parquet = derived_dir / "cells.parquet"
    out_geojson = derived_dir / "cells.geojson"

    cells.to_parquet(out_parquet)
    cells.to_file(out_geojson, driver="GeoJSON")
    log.info(f"wrote {out_parquet}")
    log.info(f"wrote {out_geojson}")

    # 6. upload to S3
    if cfg.get("s3_upload_enabled", False):
        exporter.upload_local(out_parquet, cfg["artifacts"]["files"]["cells_parquet"])
        exporter.upload_local(out_geojson, cfg["artifacts"]["files"]["cells_geojson"])

        status_doc = {
            "run_at": _now_iso(),
            "env": cfg["_env"],
            "sources": statuses,
        }
        exporter.write_status(status_doc)
    else:
        log.info("s3_upload_enabled=false — skipping upload")

    # 7. summary
    log.info("=== summary ===")
    for name, s in statuses.items():
        mark = "✓" if s["ok"] else "✗"
        log.info(f"  {mark} {name}  nulls={s['nulls']}  {s['error'] or ''}")

    failed = [n for n, s in statuses.items() if not s["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    exit_code = main()
    import logging
    logging.shutdown()
    sys.exit(exit_code)