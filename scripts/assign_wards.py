"""
One-time ward assignment for a city.

Runs WardSource for a city, saves the result to data/derived/<slug>_wards.parquet.
Run once after building the grid. Pipeline reads the parquet, doesn't re-run the join.

Usage:
    py -m scripts.assign_wards delhi
    py -m scripts.assign_wards --all      # every city with a wards file
"""

import sys
from pathlib import Path

import pandas as pd

from config import load_city, list_cities
from utils.logger import configLogger
from src.sources.wards import WardSource

log = configLogger(__file__)


def assign_for_city(slug: str) -> bool:
    cfg = load_city(slug)
    wards_file = cfg.get("raw", {}).get("wards_file")

    if not wards_file:
        log.info(f"[{slug}] no wards_file in config — skipping")
        return False

    if not Path(wards_file).exists():
        log.warning(f"[{slug}] wards_file missing on disk: {wards_file}")
        return False

    log.info(f"[{slug}] running WardSource")
    try:
        df = WardSource(cfg, log).run()
    except Exception as e:
        log.error(f"[{slug}] WardSource failed: {type(e).__name__}: {e}")
        return False

    out = Path(cfg["local"]["derived_dir"]) / f"{slug}_wards.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    log.info(f"[{slug}] wrote {out} — {len(df)} rows, cols={list(df.columns)}")
    return True


def main() -> int:
    args = sys.argv[1:]
    if not args:
        log.error("usage: py -m scripts.assign_wards <slug> | --all")
        return 1

    if args[0] == "--all":
        slugs = list_cities()
    else:
        slugs = args

    results = {slug: assign_for_city(slug) for slug in slugs}
    ok = sum(1 for v in results.values() if v)
    log.info(f"done — {ok}/{len(slugs)} cities assigned")
    return 0


if __name__ == "__main__":
    sys.exit(main())