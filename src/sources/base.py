"""
Base class for all pipeline sources.

A source is anything that produces per-hex columns for cells.parquet:
  - Sentinel-2   -> ndvi
  - WorldPop     -> population
  - OSM          -> school_count, built_frac, water_prox
  - Landsat      -> lst_c
  - Dust         -> dust_idx
  - Wards        -> ward, ward_id
  - Mock         -> all demo columns

Contract for every subclass:
  run() -> DataFrame with columns [h3_index, <source columns>]
  NO geometry in the output. Geometry comes from the grid fixture only.
"""

from logging import Logger
from pathlib import Path

import geopandas as gpd
import pandas as pd


class SourceBase:
    # Subclasses declare what columns they add. Used by tests and docs.
    column_names: list[str] = []

    def __init__(self, cfg: dict, log: Logger):
        self.cfg = cfg
        self.log = log

    def run(self) -> pd.DataFrame:
        """Produce per-hex columns. Must be implemented by subclasses."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement run()")

    def _grid(self) -> gpd.GeoDataFrame:
        """Load this city's H3 hex grid fixture."""
        path = self.cfg.get("grid_fixture_path")
        if not path:
            raise KeyError(
                "cfg['grid_fixture_path'] missing — did you load the city via load_city()?"
            )

        grid_path = Path(path)
        if not grid_path.exists():
            raise FileNotFoundError(
                f"Hex grid not found at {grid_path}. "
                f"Run `py -m scripts.build_city {self.cfg.get('_slug', '<slug>')}` first."
            )
        return gpd.read_parquet(grid_path)