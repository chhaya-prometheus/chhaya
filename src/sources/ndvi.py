"""
Sentinel-2 NDVI source.

Downloads the least-cloudy Sentinel-2 scene covering Delhi,
computes NDVI, and zonal-averages it onto the H3 hex grid.

Returns a GeoDataFrame: [h3_index, ndvi]
"""

from pathlib import Path
import hashlib
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
import stackstac
import xarray as xr
import rioxarray  
from pystac_client import Client
from rasterstats import zonal_stats

from src.sources.base import SourceBase


class Sentinel2Source(SourceBase):
    column_names = ["ndvi"]

    # ---------- public API ----------

    def run(self):
        grid = self._grid()
        scene = self._pick_scenes()
        cache = self._cache_path(scene)

        if cache.exists():
            self.log.info(f"using cached NDVI raster: {cache.name}")
            ndvi = xr.open_dataarray(cache).load()
        else:
            red, nir = self._load_bands(scene)
            ndvi = self._compute_ndvi(red, nir)
            self._save_cache(ndvi, cache)

        return self._zonal(ndvi, grid, cache)

    # ---------- internals ----------

    def _cfg(self) -> dict:
        return self.cfg["sources"]["sentinel2"]
    
    def _pick_scenes(self) -> list:
        cfg = self._cfg()
        comp = cfg["composite"]

        self.log.info(
            f"searching {cfg['collection']} — {cfg['date_range']} "
            f"— need {comp['scenes']} scenes (one per tile)"
        )

        catalog = Client.open(cfg["stac_url"])
        items = list(
            catalog.search(
                collections=[cfg["collection"]],
                bbox=cfg["bbox"],
                datetime=cfg["date_range"],
                query={"eo:cloud_cover": {"lt": cfg["max_cloud_cover"]}},
            ).item_collection()
        )

        if len(items) == 0:
            raise RuntimeError(
                f"No Sentinel-2 scenes found for {cfg['date_range']} "
                f"with cloud cover < {cfg['max_cloud_cover']}%"
            )

        # Cleanest scenes first
        items.sort(key=lambda i: i.properties.get("eo:cloud_cover", 100))

        chosen = []
        used_tiles = set()
        anchor_date = None

        for item in items:
            if len(chosen) >= comp["scenes"]:
                break

            tile = item.properties.get("grid:code", "unknown")
            if tile in used_tiles:
                continue

            if anchor_date is None:
                anchor_date = item.datetime
            elif abs((item.datetime - anchor_date).days) > comp["window_days"]:
                continue

            chosen.append(item)
            used_tiles.add(tile)

        if len(chosen) < comp["scenes"]:
            self.log.warning(
                f"only found {len(chosen)} distinct-tile scenes within "
                f"{comp['window_days']}d — proceeding with what we have"
            )

        for i in chosen:
            self.log.info(
                f"  using {i.id} — tile {i.properties.get('grid:code')} — "
                f"{i.properties['eo:cloud_cover']:.2f}% cloud — {i.datetime.date()}"
            )

        return chosen


    def _cache_path(self, scenes: list) -> Path:
        cache_dir = Path(self.cfg["local"]["raw_dir"]) / "sentinel2"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Deterministic key from sorted scene IDs
        key = hashlib.md5("|".join(sorted(s.id for s in scenes)).encode()).hexdigest()[:10]
        return cache_dir / f"composite_{key}_ndvi.tif"


    def _load_bands(self, scenes: list):
        cfg = self._cfg()
        self.log.info(f"loading {len(scenes)} scenes, bands {cfg['bands']} at {cfg['resolution_m']}m")

        stack = stackstac.stack(
            scenes,
            assets=cfg["bands"],
            epsg=cfg["epsg"],
            resolution=cfg["resolution_m"],
            bounds_latlon=cfg["bbox"],
        )  # dims: (time, band, y, x)

        red = stack.sel(band="red").compute().astype("float32")  # (time, y, x)
        nir = stack.sel(band="nir").compute().astype("float32")

        self.log.info(f"loaded red shape={red.shape}, nir shape={nir.shape}")
        return red, nir


    def _compute_ndvi(self, red, nir):
        # red, nir shape: (time, y, x). Compute NDVI per scene, then median across time.
        ndvi = (nir - red) / (nir + red)
        valid = (red > 0) & (nir > 0)
        ndvi = ndvi.where(valid)

        # Median across time — robust to a cloudy pixel in one scene
        ndvi_med = ndvi.median(dim="time", skipna=True)

        self.log.info(
            f"composite NDVI mean={float(ndvi_med.mean(skipna=True)):.3f}, "
            f"valid={float(valid.any(dim='time').mean()):.1%}"
        )
        return ndvi_med
    
    def _save_cache(self, ndvi, path: Path):
        ndvi = ndvi.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
        ndvi = ndvi.rio.write_crs(f"EPSG:{self._cfg()['epsg']}", inplace=False)
        ndvi.rio.to_raster(path)
        self.log.info(f"cached NDVI raster: {path}")

    def _zonal(self, ndvi, grid, raster_path) -> gpd.GeoDataFrame:
        # Reproject hexes to the raster's CRS (UTM) — cheap, 1,945 polygons
        grid_utm = grid.to_crs(ndvi.rio.crs)

        self.log.info(f"zonal stats over {len(grid_utm)} hexes")

        stats = zonal_stats(
            grid_utm.geometry,
            str(raster_path),
            nodata=np.nan,
            stats=["mean"],
        )

        result = gpd.GeoDataFrame(
            {
                "h3_index": grid["h3_index"].values,
                "ndvi": [s["mean"] for s in stats],
            }
        )

        non_null = result["ndvi"].notna().sum()
        self.log.info(f"zonal complete — {non_null}/{len(result)} hexes have NDVI")
        return result