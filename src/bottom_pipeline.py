"""
Multi-city slow pipeline orchestrator.

Loads each city's config, runs its sources, merges with the previous
cells.parquet from S3 (two-tier: failed sources keep old values),
computes priority + priority_rank, validates against the cell schema,
writes cells.parquet + cells.geojson locally, uploads to S3, writes
a per-city status file, and finally writes features/cities.json.

Usage:
    py -m src.bottom_pipeline                    # default: delhi
    py -m src.bottom_pipeline --city delhi
    py -m src.bottom_pipeline --city delhi --city jaipur
    py -m src.bottom_pipeline --all
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

from config import load_city, list_cities
from utils.logger import configLogger
from src.export import S3Exporter
from src.sources.ndvi import Sentinel2Source
from src.sources.stubs import (
    WorldPopStub, OSMStub, LandsatStub, DustStub,
)

log = configLogger(__file__)


# Columns every cells.parquet must have, with their default when missing.
DEFAULT_COLUMNS = {
    "ndvi": None,
    "lst_c": None,
    "population": 0,
    "built_frac": 0.0,
    "water_prox": 0.0,
    "dust_idx": 0.0,
    "school_count": 0,
    "ward": None,
    "ward_id": None,
}

# Priority weights (sum to 1.0)
PRIORITY_WEIGHTS = {
    "heat": 0.40,
    "population": 0.25,
    "greenery_deficit": 0.20,
    "dust": 0.15,
}


# --------------------------------------------------------------------------
# Source selection
# --------------------------------------------------------------------------

def pick_sources(cfg: dict) -> list:
    if cfg["data_mode"] == "demo":
        from src.sources.mock import MockSource
        return [MockSource]

    # real mode
    sources = [Sentinel2Source]
    if cfg.get("use_stubs", False):
        sources += [WorldPopStub, OSMStub, LandsatStub, DustStub]
    return sources


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_key(SourceClass) -> str:
    """Sentinel2Source -> sentinel2. WorldPopStub -> worldpop."""
    return (
        SourceClass.__name__
        .replace("Stub", "")
        .replace("Source", "")
        .lower()
    )

def run_source(SourceClass, cfg, grid) -> tuple[pd.DataFrame, dict]:
    """Run one source, catch failures, return (dataframe, status)."""
    name = SourceClass.__name__
    key = _source_key(SourceClass)
    log.info(f"[{cfg['_slug']}] --- {name} ---")

    try:
        df = SourceClass(cfg, log).run()
        source_cols = [c for c in df.columns if c != "h3_index"]
        nulls = int(df[source_cols].isna().any(axis=1).sum()) if source_cols else 0
        status = {"ok": True, "ran_at": _now_iso(), "nulls": nulls, "error": None}
        log.info(f"[{cfg['_slug']}] {name} ok — cols={source_cols}, nulls={nulls}")
        return df, status
    except Exception as e:
        log.error(f"[{cfg['_slug']}] {name} failed: {type(e).__name__}: {e}")
        empty = pd.DataFrame({"h3_index": grid["h3_index"].values})
        status = {
            "ok": False,
            "ran_at": None,
            "nulls": None,
            "error": f"{type(e).__name__}: {e}",
        }
        return empty, status


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------

def fill_defaults(cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ensure every expected column exists, with a sane default."""
    for col, default in DEFAULT_COLUMNS.items():
        if col not in cells.columns:
            cells[col] = default
    return cells


def _min_max_norm(s: pd.Series) -> pd.Series:
    """Normalize to 0..1; return zeros if constant."""
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series(0.0, index=s.index)
    return (s - lo) / (hi - lo)

def compute_priority(cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Weighted sum of:
      - heat: lst_c (higher = worse)
      - population: population (higher = worse)
      - greenery_deficit: 1 - ndvi (lower ndvi = worse)
      - dust: dust_idx (higher = worse)

    Nulls treated as column median so one missing source doesn't zero
    the score. Result is 0..1. priority_rank 1 = highest priority.
    """
    df = cells.copy()

    heat = df["lst_c"].fillna(df["lst_c"].median())
    pop = df["population"].fillna(0)
    ndvi = df["ndvi"].fillna(df["ndvi"].median())
    dust = df["dust_idx"].fillna(df["dust_idx"].median())

    heat_n = _min_max_norm(heat)
    pop_n = _min_max_norm(pop)
    green_n = 1.0 - _min_max_norm(ndvi)
    dust_n = _min_max_norm(dust)

    df["priority"] = (
        PRIORITY_WEIGHTS["heat"] * heat_n
        + PRIORITY_WEIGHTS["population"] * pop_n
        + PRIORITY_WEIGHTS["greenery_deficit"] * green_n
        + PRIORITY_WEIGHTS["dust"] * dust_n
    ).clip(0, 1)

    df["priority_rank"] = (
        df["priority"]
        .rank(ascending=False, method="first")
        .astype(int)
    )
    return df


def validate_schema(cells: gpd.GeoDataFrame, schema_path: Path) -> tuple[bool, str]:
    """Validate cells against cell.schema.json using jsonschema."""
    try:
        import jsonschema
    except ImportError:
        return True, "jsonschema not installed — skipping validation"

    schema = json.loads(schema_path.read_text())

    # Convert to plain records for validation
    records = cells.drop(columns=["geometry"], errors="ignore").to_dict("records")

    for i, row in enumerate(records):
        try:
            jsonschema.validate(instance=row, schema=schema)
        except jsonschema.ValidationError as e:
            return False, f"row {i} (h3={row.get('h3_index')}): {e.message}"

    return True, f"{len(records)} rows valid"

# --------------------------------------------------------------------------
# Wards
# --------------------------------------------------------------------------

def load_wards_fixture(cfg: dict) -> pd.DataFrame | None:
    """Load <slug>_wards.parquet if it exists, else None."""
    path = Path(cfg["local"]["derived_dir"]) / f"{cfg['_slug']}_wards.parquet"
    if not path.exists():
        return None
    log.info(f"[{cfg['_slug']}] loading wards fixture: {path.name}")
    return pd.read_parquet(path)


# --------------------------------------------------------------------------
# Per-city pipeline
# --------------------------------------------------------------------------

def run_city(slug: str) -> tuple[bool, dict | None]:
    """
    Run the full pipeline for one city.

    Returns (ok, city_index_entry). entry is None if city failed.
    """
    cfg = load_city(slug)
    log.info("=" * 60)
    log.info(f"  CITY: {cfg['name']} ({slug})  mode={cfg['data_mode']}")
    log.info(f"  env={cfg['_env']}  bucket={cfg['aws']['bucket']}")
    log.info("=" * 60)

    derived_dir = Path(cfg["local"]["derived_dir"])
    derived_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load grid fixture
    grid_path = Path(cfg["grid_fixture_path"])
    if not grid_path.exists():
        log.error(f"[{slug}] grid fixture missing: {grid_path}")
        return False, None
    grid = gpd.read_parquet(grid_path)
    log.info(f"[{slug}] grid: {len(grid)} hexes")

    # 2. Load existing cells from S3 (None on first run)
    exporter = S3Exporter(cfg, log)
    old_cells = exporter.load_existing_cells() if cfg.get("s3_upload_enabled") else None

    # 3. Load wards fixture if exists
    wards = load_wards_fixture(cfg)

    # 4. Run sources
    cells = grid[["h3_index", "geometry"]].copy()
    statuses: dict[str, dict] = {}
    ok_columns: set[str] = set()

    for SourceClass in pick_sources(cfg):
        col_df, status = run_source(SourceClass, cfg, grid)
        statuses[_source_key(SourceClass)] = status
        if status["ok"]:
            ok_columns.update(c for c in col_df.columns if c != "h3_index")
        cells = cells.merge(col_df, on="h3_index", how="left")

    # 5. Merge wards in (not a source, but a fixture)
    if wards is not None:
        cells = cells.merge(wards[["h3_index", "ward", "ward_id"]], on="h3_index", how="left")
        ok_columns.update(["ward", "ward_id"])
        log.info(f"[{slug}] wards merged")
    else:
        cells["ward"] = None
        cells["ward_id"] = None

    # 6. Two-tier merge with previous run
    cells = exporter.merge_preserving_old(cells, old_cells, ok_columns)

    # 7. Fill defaults, compute priority
    cells = fill_defaults(cells)
    cells = compute_priority(cells)

    log.info(f"[{slug}] final — {len(cells)} rows, cols={list(cells.columns)}")

    # 8. Validate against schema
    schema_path = Path(cfg["_project_root"]) / "contract" / "cell.schema.json"
    ok, msg = validate_schema(cells, schema_path)
    if not ok:
        log.error(f"[{slug}] SCHEMA VALIDATION FAILED: {msg}")
        log.error(f"[{slug}] aborting — not uploading")
        return False, None
    log.info(f"[{slug}] schema valid: {msg}")

    # 9. Write locally
    out_parquet = derived_dir / f"{slug}_cells.parquet"
    out_geojson = derived_dir / f"{slug}_cells.geojson"
    cells.to_parquet(out_parquet)
    cells.to_file(out_geojson, driver="GeoJSON")
    log.info(f"[{slug}] wrote {out_parquet.name}, {out_geojson.name}")

    # 10. Upload
    if cfg.get("s3_upload_enabled", False):
        if cfg["_env"] == "prod" and not cfg.get("confirm_prod_upload", False):
            log.error(f"[{slug}] prod upload not confirmed — set confirm_prod_upload")
            return False, None

        exporter.upload_local(out_parquet, exporter._key_cells_parquet())
        exporter.upload_local(out_geojson, exporter._key_cells_geojson())

        status_doc = {
            "run_at": _now_iso(),
            "env": cfg["_env"],
            "city": slug,
            "data_mode": cfg["data_mode"],
            "sources": statuses,
        }
        exporter.write_status(status_doc)
        log.info(f"[{slug}] uploaded cells + status to S3")
    else:
        log.info(f"[{slug}] s3_upload_enabled=false — local only")

    # 11. Summary
    log.info(f"[{slug}] === summary ===")
    for name, s in statuses.items():
        mark = "✓" if s["ok"] else "✗"
        log.info(f"  {mark} {name}  nulls={s['nulls']}  {s['error'] or ''}")

    # Build cities index entry
    b = grid.total_bounds
    entry = {
        "slug": slug,
        "name": cfg["name"],
        "bbox": [float(b[0]), float(b[1]), float(b[2]), float(b[3])],
        "center": [float((b[0] + b[2]) / 2), float((b[1] + b[3]) / 2)],
        "zoom": 11,
        "data_mode": cfg["data_mode"],
        "has_wards": wards is not None,
    }

    failed = [n for n, s in statuses.items() if not s["ok"]]
    return (len(failed) == 0), entry

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", action="append", default=None,
                        help="City slug (repeatable). Defaults to delhi.")
    parser.add_argument("--all", action="store_true",
                        help="Run every city in config/cities/")
    args = parser.parse_args()

    if args.all:
        slugs = list_cities()
    elif args.city:
        slugs = args.city
    else:
        slugs = ["delhi"]

    log.info(f"running for cities: {slugs}")

    results: dict[str, bool] = {}
    entries: list[dict] = []

    for slug in slugs:
        try:
            ok, entry = run_city(slug)
        except Exception as e:
            log.error(f"[{slug}] pipeline crashed: {type(e).__name__}: {e}")
            ok, entry = False, None
        results[slug] = ok
        if entry:
            entries.append(entry)

    # Update cities.json — merge with existing entries for cities not in this run
    if entries:
        try:
            any_cfg = load_city(slugs[0])
            if any_cfg.get("s3_upload_enabled", False):
                exporter = S3Exporter(any_cfg, log)
                existing = exporter.load_cities_index()
                keep = {e["slug"]: e for e in existing if e.get("slug")}
                for e in entries:
                    keep[e["slug"]] = e
                merged = list(keep.values())
                exporter.write_cities_index(merged)
        except Exception as e:
            log.error(f"cities.json update failed: {type(e).__name__}: {e}")

    log.info("=== final ===")
    for slug, ok in results.items():
        log.info(f"  {'✓' if ok else '✗'} {slug}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    exit_code = main()
    logging.shutdown()
    sys.exit(exit_code)