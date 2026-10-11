# src/sources/wards.py
"""
Delhi ward assignment source.

Assigns every hex to a ward by largest spatial overlap.
Hexes with <20% overlap go to the nearest ward within 500m.
Hexes with no ward within 500m get null.

Returns [h3_index, ward, ward_id] — no geometry.
"""

import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.validation import make_valid

from src.sources.base import SourceBase


OVERLAP_THRESHOLD = 0.20   # if largest overlap < 20% of hex area, use nearest
NEAREST_MAX_METERS = 500   # hexes beyond this from any ward stay null


class WardSource(SourceBase):
    column_names = ["ward", "ward_id"]

    def __init__(self, cfg: dict, log):
        super().__init__(cfg, log)
        self.city = cfg["_slug"]

    def run(self) -> pd.DataFrame:
        wards_path = Path(self.cfg["raw"]["wards_file"])
        if not wards_path.exists():
            raise FileNotFoundError(f"Wards file not found: {wards_path}")

        self.log.info(f"loading wards: {wards_path}")
        wards = gpd.read_file(wards_path)

        grid = self._grid()
        self.log.info(f"grid: {len(grid)} hexes, wards: {len(wards)}")

        # Build stable ward_id with dedup, and display name
        wards = self._prepare_wards(wards)

        # Reproject both to UTM for area-accurate overlap
        utm = grid.estimate_utm_crs()
        self.log.info(f"reprojecting to {utm}")
        grid_utm = grid.to_crs(utm)
        wards_utm = wards.to_crs(utm)

        # Largest-overlap assignment
        assigned = self._assign_by_overlap(grid_utm, wards_utm)

        # Fallback: nearest ward within 500m for low-overlap hexes
        assigned = self._fill_by_nearest(assigned, grid_utm, wards_utm)

        # Back to original CRS, keep only what we return
        assigned = assigned.to_crs(grid.crs)

        # Report
        self._log_summary(assigned, wards)

        return pd.DataFrame(assigned[["h3_index", "ward", "ward_id"]])

    # ---------- internals ----------

    def _prepare_wards(self, wards: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        name_col = self.cfg["raw"]["ward_name_col"]
        id_col = self.cfg["raw"]["ward_id_col"]

        wards = wards.copy()
        wards["geometry"] = wards["geometry"].apply(make_valid)

        # Display name
        wards["ward_name"] = wards[name_col].astype(str).str.strip().str.title()

        # Slug base
        slug_base = (
            wards[id_col]
            .astype(str)
            .str.strip()
            .str.lower()
            .str.replace(r"[^a-z0-9]+", "-", regex=True)
            .str.strip("-")
        )

        # Deduplicate: append -2, -3, ... only where needed
        seen: dict[str, int] = {}
        ward_ids = []
        for base in slug_base:
            if base not in seen:
                seen[base] = 1
                ward_ids.append(base)
            else:
                seen[base] += 1
                ward_ids.append(f"{base}-{seen[base]}")
        wards["ward_id"] = ward_ids

        # Warn if any ward_id was deduped
        dup_base_count = sum(1 for v in seen.values() if v > 1)
        if dup_base_count:
            self.log.warning(
                f"{dup_base_count} ward_id(s) collided and were suffixed with -2, -3, ..."
            )

        return wards

    def _assign_by_overlap(self, grid_utm, wards_utm) -> gpd.GeoDataFrame:
        # Spatial join: candidate pairs only (fast), then compute overlap area
        joined = gpd.sjoin(grid_utm, wards_utm, how="left", predicate="intersects")

        # Compute overlap area per (hex, ward) pair
        joined = joined.reset_index().rename(columns={"index": "hex_idx"})

        # Join won't include geometry of wards; fetch via merge
        ward_geoms = wards_utm[["ward_id", "ward_name", "geometry"]].rename(
            columns={"geometry": "ward_geom"}
        )
        joined = joined.merge(ward_geoms, on=["ward_id", "ward_name"], how="left")

        # Overlap area
        joined["overlap_m2"] = joined.apply(
            lambda r: r.geometry.intersection(r.ward_geom).area
            if r.ward_geom is not None and r.ward_geom is not pd.NA
            else 0.0,
            axis=1,
        )

        # Hex area for threshold
        joined["hex_area_m2"] = joined.geometry.area

        # Pick the ward with largest overlap per hex
        joined = joined.sort_values("overlap_m2", ascending=False)
        best = joined.drop_duplicates(subset="hex_idx", keep="first").copy()

        # Ambiguity flag
        best["overlap_frac"] = best["overlap_m2"] / best["hex_area_m2"]
        low_overlap = (best["overlap_frac"] < OVERLAP_THRESHOLD).sum()
        self.log.info(
            f"{low_overlap} hexes with overlap < {OVERLAP_THRESHOLD:.0%} "
            f"— will use nearest-ward fallback"
        )
        best["_needs_fallback"] = best["overlap_frac"] < OVERLAP_THRESHOLD

        return best

    def _fill_by_nearest(self, assigned, grid_utm, wards_utm) -> gpd.GeoDataFrame:
        needs = assigned[assigned["_needs_fallback"]].copy()
        if len(needs) == 0:
            assigned["ward"] = assigned["ward_name"]
            assigned["ward_id"] = assigned["ward_id"]
            return assigned

        # For each hex needing fallback, find nearest ward centroid
        ward_centroids = wards_utm.copy()
        ward_centroids["geometry"] = ward_centroids.geometry.centroid

        joined = gpd.sjoin_nearest(
            needs[["hex_idx", "geometry"]],
            ward_centroids[["ward_id", "ward_name", "geometry"]],
            how="left",
            max_distance=NEAREST_MAX_METERS,
            distance_col="distance_m",
        )

        # Fill from the nearest result
        joined = joined.drop_duplicates(subset="hex_idx", keep="first")

        fill = joined.set_index("hex_idx")[["ward_name", "ward_id"]].rename(
            columns={"ward_name": "ward_filled", "ward_id": "ward_id_filled"}
        )

        assigned = assigned.merge(fill, left_on="hex_idx", right_index=True, how="left")
        mask = assigned["_needs_fallback"]
        assigned.loc[mask, "ward_name"] = assigned.loc[mask, "ward_filled"]
        assigned.loc[mask, "ward_id"] = assigned.loc[mask, "ward_id_filled"]

        reassigned = assigned.loc[mask, "ward_id_filled"].notna().sum()
        self.log.info(f"fallback reassigned {reassigned}/{mask.sum()} low-overlap hexes")

        assigned["ward"] = assigned["ward_name"]
        return assigned

    def _log_summary(self, assigned, wards):
        total = len(assigned)
        unassigned = assigned["ward"].isna().sum()
        self.log.info(f"assigned: {total - unassigned}/{total} hexes")
        self.log.info(f"unassigned: {unassigned}")

        ward_counts = assigned.groupby("ward_id").size()
        zero_wards = wards[~wards["ward_id"].isin(ward_counts.index)]
        self.log.info(f"wards with zero hexes: {len(zero_wards)}")
        if len(zero_wards) > 0:
            names = ", ".join(zero_wards["ward_name"].head(20).tolist())
            self.log.info(f"  {names}{' ...' if len(zero_wards) > 20 else ''}")