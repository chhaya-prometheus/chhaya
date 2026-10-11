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
    """
    Return the list of source classes to run for this city.

    - All cities get Sentinel2Source (real NDVI download).
    - If use_stubs is true, add the four stub sources.
    - Wards are NOT a source here; they are loaded from the
      one-time <slug>_wards.parquet fixture.
    """
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

