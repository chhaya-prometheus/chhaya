"""
Build a city's H3 hex grid fixture from its boundary file.

Input:  <cities_dir>/<slug>_boundary.geojson  (declared in config/cities/<slug>.yaml)
Output: <cities_dir>/<slug>_hex_res8.parquet  (cols: h3_index, geometry)

Idempotent — skips if output exists, unless --force is passed.
Hard-fails if the boundary file is missing.

Usage:
    py -m scripts.build_city delhi
    py -m scripts.build_city jaipur chandigarh
    py -m scripts.build_city --all
    py -m scripts.build_city --all --force
"""

import sys
from pathlib import Path

import geopandas as gpd
import h3
from shapely.geometry import Polygon
from shapely.validation import make_valid

from config import load_city, list_cities
from utils.logger import configLogger

log = configLogger(__file__)

MIN_CELLS = 100
MAX_CELLS = 20_000


def _hex_to_polygon(h: str) -> Polygon:
    """H3 4.x cell_to_boundary returns ((lat, lng), ...) — shapely wants (lng, lat)."""
    boundary = h3.cell_to_boundary(h)
    return Polygon([(lng, lat) for lat, lng in boundary])


def _load_boundary(cfg: dict) -> gpd.GeoDataFrame:
    raw = cfg.get("raw", {})
    boundary_path = raw.get("boundary")
    if not boundary_path:
        raise KeyError(f"cfg['raw']['boundary'] missing for {cfg['_slug']}")

    path = Path(boundary_path)
    if not path.exists():
        raise FileNotFoundError(f"Boundary file not found: {path}")

    log.info(f"[{cfg['_slug']}] reading boundary: {path.name}")
    gdf = gpd.read_file(path)

    if gdf.empty:
        raise ValueError(f"Boundary file has no features: {path}")

    return gdf


def _dissolve_to_polygon(gdf: gpd.GeoDataFrame) -> Polygon:
    """Reproject to 4326, make_valid, dissolve to one polygon."""
    gdf = gdf.to_crs(epsg=4326)
    gdf["geometry"] = gdf.geometry.apply(make_valid)
    dissolved = gdf.geometry.union_all()
    if dissolved.is_empty:
        raise ValueError("Dissolved boundary is empty")
    return dissolved


def build_for_city(slug: str, force: bool = False) -> bool:
    cfg = load_city(slug)
    cities_dir = Path(cfg["local"]["cities_dir"])
    out_path = cities_dir / f"{slug}_hex_res8.parquet"

    if out_path.exists() and not force:
        log.info(f"[{slug}] fixture exists, skipping: {out_path.name}")
        return True

    log.info(f"[{slug}] building hex grid")

    # Boundary
    gdf = _load_boundary(cfg)
    polygon = _dissolve_to_polygon(gdf)
    log.info(f"[{slug}] boundary dissolved — type={polygon.geom_type}")

    # Fill with hexagons
    res = cfg["h3_res"]
    hex_ids = h3.geo_to_cells(polygon.__geo_interface__, res=res)
    log.info(f"[{slug}] h3 res={res} — {len(hex_ids)} cells")

    # Sanity warnings
    if len(hex_ids) < MIN_CELLS:
        log.warning(f"[{slug}] only {len(hex_ids)} cells — is the boundary too small?")
    if len(hex_ids) > MAX_CELLS:
        log.warning(f"[{slug}] {len(hex_ids)} cells — large grid, may be slow")

    # Build GeoDataFrame
    hex_ids_sorted = sorted(hex_ids)
    geometries = [_hex_to_polygon(h) for h in hex_ids_sorted]
    hex_gdf = gpd.GeoDataFrame(
        {"h3_index": hex_ids_sorted},
        geometry=geometries,
        crs="EPSG:4326",
    )

    # Bounds sanity
    b = hex_gdf.total_bounds
    log.info(f"[{slug}] bounds: [{b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f}]")

    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hex_gdf.to_parquet(out_path)
    size_kb = out_path.stat().st_size / 1024
    log.info(f"[{slug}] wrote {out_path.name} ({size_kb:.1f} KB, {len(hex_gdf)} rows)")

    return True


def main() -> int:
    args = sys.argv[1:]
    if not args:
        log.error("usage: py -m scripts.build_city <slug> [<slug> ...] | --all [--force]")
        return 1

    force = "--force" in args
    args = [a for a in args if a != "--force"]

    if not args:
        log.error("no slugs provided")
        return 1

    if args[0] == "--all":
        slugs = list_cities()
    else:
        slugs = args

    log.info(f"building grid for: {slugs}  (force={force})")

    results = {}
    for slug in slugs:
        try:
            results[slug] = build_for_city(slug, force=force)
        except Exception as e:
            log.error(f"[{slug}] failed: {type(e).__name__}: {e}")
            results[slug] = False

    log.info("=== summary ===")
    for slug, ok in results.items():
        log.info(f"  {'✓' if ok else '✗'} {slug}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())