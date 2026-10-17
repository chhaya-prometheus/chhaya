"""
Validate the Sentinel-2 → NDVI → H3 pipeline for a city.

Run:
    py -m tests.validate
    py -m tests.validate --city delhi --no-cache
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import xarray as xr

from config import load_city, list_cities
from src.sources.ndvi import Sentinel2Source
from utils.logger import configLogger


EXPECTED_BY_CITY = {
    "delhi": {
        "min_side_km":      20,
        "max_side_km":     100,
        "min_valid_frac":    0.95,
        "min_ndvi_frac":     0.75,
        "min_zonal_frac":    0.95,
        "ndvi_median_lo":   -0.2,
        "ndvi_median_hi":    0.8,
    },
}

DEFAULT = {
    "min_side_km":      10,
    "max_side_km":     200,
    "min_valid_frac":    0.85,
    "min_ndvi_frac":     0.70,
    "min_zonal_frac":    0.80,
    "ndvi_median_lo":   -0.3,
    "ndvi_median_hi":    0.9,
}


class Checks:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    def summary(self) -> bool:
        n = len(self.results)
        n_pass = sum(1 for _, ok, _ in self.results if ok)
        print(f"\n{'='*60}")
        print(f"{n_pass}/{n} checks passed, {n-n_pass} failed")
        print(f"{'='*60}")
        return n_pass == n


def valid_mask(red: xr.DataArray, nir: xr.DataArray, eps: float = 1e-6):
    denom = nir + red
    return np.isfinite(red) & np.isfinite(nir) & (np.abs(denom) > eps)


def check_config(cfg: dict, c: Checks):
    print("\n[1] Config sanity")
    s2 = cfg["sources"]["sentinel2"]

    c.check("bbox present", "bbox" in s2 and len(s2["bbox"]) == 4, f"bbox={s2.get('bbox')}")
    c.check("epsg is projected", int(s2.get("epsg", 0)) >= 2000, f"epsg={s2.get('epsg')}")
    c.check("date_range present", "date_range" in s2, f"date_range={s2.get('date_range')}")
    c.check("resolution_m in {10,20,60}", s2.get("resolution_m") in (10, 20, 60), f"res={s2.get('resolution_m')}")
    c.check("bands include red+nir", {"red", "nir"}.issubset(set(s2.get("bands", []))), f"bands={s2.get('bands')}")


def check_scenes(scenes: list, cfg: dict, c: Checks):
    print("\n[2] Scene selection")
    s2 = cfg["sources"]["sentinel2"]
    comp = s2["composite"]

    c.check("≥1 scene selected", len(scenes) >= 1, f"n={len(scenes)}")

    tiles = [s.properties.get("grid:code", "unknown") for s in scenes]
    c.check("tiles are distinct", len(tiles) == len(set(tiles)), f"tiles={tiles}")

    if scenes:
        dates = [s.datetime for s in scenes]
        span = (max(dates) - min(dates)).days
        c.check("scene date span ≤ window_days", span <= comp["window_days"], f"span={span}d")

        clouds = [s.properties.get("eo:cloud_cover", 100.0) for s in scenes]
        c.check("all scenes ≤ max_cloud_cover", all(cl <= s2["max_cloud_cover"] for cl in clouds),
                f"clouds={[f'{cl:.1f}%' for cl in clouds]}")


def check_grid(red: xr.DataArray, cfg: dict, c: Checks):
    print("\n[3] Grid")
    exp = EXPECTED_BY_CITY.get(cfg.get("_slug", ""), DEFAULT)
    res = cfg["sources"]["sentinel2"]["resolution_m"]

    if not ({"y", "x"} <= set(red.dims)):
        c.check("grid has y,x dims", False, f"dims={tuple(red.dims)}")
        return

    y, x = red.sizes["y"], red.sizes["x"]
    h_km, w_km = y * res / 1000, x * res / 1000

    c.check(f"height in [{exp['min_side_km']}, {exp['max_side_km']}] km",
            exp["min_side_km"] <= h_km <= exp["max_side_km"], f"{h_km:.1f}km")
    c.check(f"width in [{exp['min_side_km']}, {exp['max_side_km']}] km",
            exp["min_side_km"] <= w_km <= exp["max_side_km"], f"{w_km:.1f}km")


def check_coverage(red: xr.DataArray, nir: xr.DataArray, cfg: dict, c: Checks):
    print("\n[4] Spatial coverage")
    exp = EXPECTED_BY_CITY.get(cfg.get("_slug", ""), DEFAULT)

    valid = valid_mask(red, nir).any(dim="time")
    frac = float(valid.mean())
    c.check(f"≥{exp['min_valid_frac']:.0%} of pixels covered by ≥1 scene",
            frac >= exp["min_valid_frac"], f"coverage={frac:.1%}")

    per_scene = valid_mask(red, nir).mean(dim=("y", "x")).values
    c.check("every scene contributes ≥1% of pixels", all(v >= 0.01 for v in per_scene),
            f"per-scene={[f'{v:.1%}' for v in per_scene]}")


def check_ndvi(ndvi: xr.DataArray, cfg: dict, c: Checks):
    print("\n[5] NDVI validity")
    exp = EXPECTED_BY_CITY.get(cfg.get("_slug", ""), DEFAULT)

    valid_frac = float(ndvi.notnull().mean())
    c.check(f"≥{exp['min_ndvi_frac']:.0%} of pixels have NDVI",
            valid_frac >= exp["min_ndvi_frac"], f"valid={valid_frac:.1%}")

    finite = ndvi.where(np.isfinite(ndvi))
    vmin = float(finite.min(skipna=True))
    vmax = float(finite.max(skipna=True))
    vmed = float(finite.median(skipna=True))

    c.check("NDVI within [-1, 1]", -1.0 <= vmin and vmax <= 1.0, f"range=[{vmin:.3f}, {vmax:.3f}]")
    c.check(f"NDVI median in [{exp['ndvi_median_lo']}, {exp['ndvi_median_hi']}]",
            exp["ndvi_median_lo"] <= vmed <= exp["ndvi_median_hi"], f"median={vmed:.3f}")
    c.check("NDVI not constant", not np.isclose(vmin, vmax), f"range=[{vmin:.3f}, {vmax:.3f}]")


def check_zonal(result, grid, cfg: dict, c: Checks):
    print("\n[6] Zonal output")
    exp = EXPECTED_BY_CITY.get(cfg.get("_slug", ""), DEFAULT)

    c.check("has h3_index + ndvi columns", {"h3_index", "ndvi"}.issubset(result.columns),
            f"columns={list(result.columns)}")
    c.check("row count matches grid", len(result) == len(grid), f"{len(result)} vs {len(grid)}")

    non_null = int(result["ndvi"].notna().sum())
    frac = non_null / len(result) if len(result) else 0.0
    c.check(f"≥{exp['min_zonal_frac']:.0%} of hexes have NDVI",
            frac >= exp["min_zonal_frac"], f"{non_null}/{len(result)} = {frac:.1%}")


def diagnostic(red: xr.DataArray, nir: xr.DataArray):
    print("\n" + "-" * 60)
    print("DIAGNOSTIC")
    print("-" * 60)

    vm = valid_mask(red, nir)
    for t in range(red.sizes["time"]):
        print(f"  scene {t}: {float(vm.isel(time=t).mean()):6.1%} valid")

    union = vm.any(dim="time")
    print(f"  union across time: {float(union.mean()):.1%}")

    missing = ~union.values
    if not missing.any():
        print("  no missing pixels")
        print("-" * 60 + "\n")
        return

    h, w = missing.shape
    print(f"  missing: top={missing[:h//2].mean():.1%} bot={missing[h//2:].mean():.1%} "
          f"left={missing[:, :w//2].mean():.1%} right={missing[:, w//2:].mean():.1%}")

    ys, xs = np.where(missing)
    y, x = int(ys[0]), int(xs[0])
    vals = []
    for t in range(red.sizes["time"]):
        r, n = float(red.isel(time=t, y=y, x=x)), float(nir.isel(time=t, y=y, x=x))
        vals.append(f"({r:+.2f},{n:+.2f})")
    print(f"  sample missing ({y},{x}): {' '.join(vals)}")
    print("-" * 60 + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="delhi")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    if args.city not in list_cities():
        print(f"ERROR: unknown city '{args.city}'. Available: {list_cities()}")
        return 2

    cfg = load_city(args.city)
    src = Sentinel2Source(cfg, configLogger(__file__))
    checks = Checks()

    print(f"Validating Sentinel-2 pipeline for '{args.city}'")

    check_config(cfg, checks)
    scenes = src._pick_scenes()
    check_scenes(scenes, cfg, checks)

    red, nir = src._load_bands(scenes)
    diagnostic(red, nir)

    check_grid(red, cfg, checks)
    check_coverage(red, nir, cfg, checks)

    ndvi = src._compute_ndvi(red, nir)
    check_ndvi(ndvi, cfg, checks)

    grid = src._grid()
    cache = src._cache_path(scenes)
    if args.no_cache and cache.exists():
        cache.unlink()
    if not cache.exists():
        src._save_cache(ndvi, cache)
    result = src._zonal(ndvi, grid, cache)
    check_zonal(result, grid, cfg, checks)

    return 0 if checks.summary() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))