"""
Spot-check NDVI against reference land-cover types in Delhi.

Usage:
    py -m scripts.spot_check
    py -m scripts.spot_check --city delhi
    py -m scripts.spot_check --grid   # show 5x5 grid for every point
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import xarray as xr
from pyproj import Transformer

from config import load_city, list_cities
from src.sources.ndvi  import Sentinel2Source
from utils.logger import configLogger
import rioxarray



DELHI_POINTS = [
    # Open water — must hit main channel, not bank.
    # Nizamuddin, but ~200m west to clear the bank vegetation.
    ("Yamuna river (Okhla Barrage)", 28.5470, 77.2950, -0.15, +0.15),

    # Clear sandbar — south of Nizamuddin, away from vegetation.
    # This is the wide sandy stretch near Okhla bird sanctuary.
    ("Yamuna sandbar (Okhla)",     28.5450, 77.2930,  0.00, 0.20),

    # Dense forest — Buddha Jayanti south edge, sun-facing.
    ("Delhi Ridge (Buddha Jayanti)", 28.5450, 77.1600, 0.55, 0.95),

    # Urban — the one thing that's already PASSING.
    ("Connaught Place (urban)",    28.6315, 77.2167,  0.05, 0.25),
]


def lat_lon_to_pixel(lat, lon, ndvi):
    epsg = ndvi.rio.crs.to_epsg()
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x, y = tr.transform(lon, lat)
    col = int(np.argmin(np.abs(ndvi.x.values - x)))
    row = int(np.argmin(np.abs(ndvi.y.values - y)))
    return row, col


def check_point(name, lat, lon, lo, hi, ndvi):
    row, col = lat_lon_to_pixel(lat, lon, ndvi)
    arr = ndvi.values                         # numpy, shape (ny, nx)
    val = float(arr[row, col])
    ok = lo <= val <= hi
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name:<30} ({lat:.4f},{lon:.4f}) → ndvi={val:+.3f}  "
          f"[expected {lo:+.2f}..{hi:+.2f}]")
    return ok, row, col


def show_grid(ndvi, row, col, size=5, label=""):
    """5x5 NDVI grid centered on (row, col)."""
    arr = ndvi.values
    ny, nx = arr.shape
    half = size // 2
    print(f"\n  {label} 5×5 grid:")
    finite = []
    for dy in range(-half, half + 1):
        row_vals = []
        for dx in range(-half, half + 1):
            y, x = row + dy, col + dx
            if 0 <= y < ny and 0 <= x < nx:
                v = float(arr[y, x])
                row_vals.append(f"{v:+.2f}" if np.isfinite(v) else " nan ")
                if np.isfinite(v):
                    finite.append(v)
            else:
                row_vals.append(" --- ")
        print(f"    {' '.join(row_vals)}")
    if finite:
        print(f"    min={min(finite):+.2f}  med={np.median(finite):+.2f}  "
              f"max={max(finite):+.2f}  valid={len(finite)}/{size*size}")


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--city", default="delhi")
    p.add_argument("--grid", action="store_true",
                   help="show 5x5 grid for every point (verbose)")
    args = p.parse_args(argv)

    if args.city not in list_cities():
        print(f"ERROR: unknown city '{args.city}'. Available: {list_cities()}")
        return 2

    cfg = load_city(args.city)
    src = Sentinel2Source(cfg, configLogger(__file__))

    scenes = src._pick_scenes()
    cache = src._cache_path(scenes)
    if cache.exists():
        print(f"Loading cached NDVI: {cache.name}")
        ndvi = rioxarray.open_rasterio(cache).squeeze(drop=True)
    else:
        print("Cache missing — computing from scratch")
        red, nir = src._load_bands(scenes)
        ndvi = src._compute_ndvi(red, nir)
        src._save_cache(ndvi, cache)

    if ndvi.rio.crs is None:
        ndvi = ndvi.rio.write_crs(f"EPSG:{cfg['sources']['sentinel2']['epsg']}")

    points = DELHI_POINTS if args.city == "delhi" else []
    if not points:
        print(f"No reference points defined for '{args.city}'")
        return 1

    print(f"\nSpot-checking {len(points)} reference points in {args.city}:\n")

    results = []
    coords = []
    for name, lat, lon, lo, hi in points:
        ok, row, col = check_point(name, lat, lon, lo, hi, ndvi)
        results.append(ok)
        coords.append((name, row, col, ok))

    # Show grids for failures always, for all points if --grid
    needs_grid = [(n, r, c) for n, r, c, ok in coords if args.grid or not ok]
    if needs_grid:
        print(f"\nGrids for {'all' if args.grid else 'failed'} points:")
        for name, row, col in needs_grid:
            show_grid(ndvi, row, col, label=name)

    n_pass = sum(results)
    n = len(results)
    print(f"\n{'='*60}")
    print(f"{n_pass}/{n} reference points passed")
    print(f"{'='*60}")
    return 0 if n_pass == n else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))