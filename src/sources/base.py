"""
Base class for all monthly pipeline sources.

A source is anything that produces per-hex columns for cells.parquet:
  - Sentinel-2   → ndvi
  - WorldPop     → population
  - OSM          → school_count, built_frac, water_prox
  - Landsat      → lst_c
  - Dust         → dust_idx

Contract for every subclass:
  run() -> GeoDataFrame with columns [h3_index, <source columns>]
"""

from logging import Logger
from geopandas import GeoDataFrame


class SourceBase:
    # Subclasses declare what columns they add. Used by tests and docs.
    column_names: list[str] = []

    def __init__(self, cfg: dict, log: Logger):
        self.cfg = cfg
        self.log = log

    def run(self) -> GeoDataFrame:
        """Produce per-hex columns. Must be implemented by subclasses."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement run()")

    def _grid(self) -> GeoDataFrame:
        """Load the H3 hex grid fixture from local disk."""
        import geopandas as gpd
        from pathlib import Path

        grid_path = Path(self.cfg["fixtures"]["local_dir"]) / "delhi_hex_res8.parquet"
        if not grid_path.exists():
            raise FileNotFoundError(
                f"Hex grid not found at {grid_path}. "
                f"Run `py -m src.upload_fixtures` first, or pull it from S3."
            )
        return gpd.read_parquet(grid_path)