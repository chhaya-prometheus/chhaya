"""
S3 read/write for pipeline artifacts.

Only this module touches S3. Everything else writes to local disk.
Handles the two-tier merge: new source columns overwrite old values,
failed sources keep their previous values.
"""

from pathlib import Path
import io
import json

import boto3
import geopandas as gpd
import pandas as pd
from botocore.exceptions import ClientError


class S3Exporter:
    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.bucket = cfg["aws"]["bucket"]
        self.region = cfg["aws"]["region"]
        self.client = boto3.client("s3", region_name=self.region)

    # ---------- read ----------

    def load_existing_cells(self) -> gpd.GeoDataFrame | None:
        """Fetch previous cells.parquet from S3, or None if first run."""
        key = self.cfg["artifacts"]["files"]["cells_parquet"]
        try:
            self.log.info(f"reading s3://{self.bucket}/{key}")
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            gdf = gpd.read_parquet(io.BytesIO(obj["Body"].read()))
            self.log.info(f"loaded existing cells.parquet — {len(gdf)} rows")
            return gdf
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                self.log.info("no existing cells.parquet — first run")
                return None
            raise

    # ---------- two-tier merge ----------

    def merge_preserving_old(
        self,
        new: gpd.GeoDataFrame,
        old: gpd.GeoDataFrame | None,
        ok_columns: set[str],
    ) -> gpd.GeoDataFrame:
        """
        For columns in ok_columns, use new values.
        For everything else, keep old values (if old exists).
        """
        if old is None:
            return new

        # Start from new (which has fresh values for ok sources)
        merged = new.copy()

        # For columns NOT in ok_columns, restore old values where new has nulls
        for col in old.columns:
            if col in ("h3_index", "geometry"):
                continue
            if col in ok_columns:
                continue
            if col not in merged.columns:
                merged[col] = None

            # Take old value where new is null
            old_indexed = old.set_index("h3_index")[col]
            merged_indexed = merged.set_index("h3_index")
            merged_indexed[col] = merged_indexed[col].fillna(old_indexed)
            merged = merged_indexed.reset_index()
            merged = gpd.GeoDataFrame(merged, geometry="geometry", crs=new.crs)

        return merged

    # ---------- write ----------

    def upload_local(self, local_path: Path, s3_key: str):
        size_kb = local_path.stat().st_size / 1024
        self.log.info(f"uploading {local_path.name} ({size_kb:.1f} KB) → s3://{self.bucket}/{s3_key}")
        try:
            self.client.upload_file(str(local_path), self.bucket, s3_key)
            self.log.info(f"  ✓ {s3_key}")
        except ClientError as e:
            self.log.error(f"  ✗ {e}")
            raise

    def write_status(self, status: dict):
        key = self.cfg["artifacts"]["files"]["pipeline_status"]
        self.log.info(f"writing status → s3://{self.bucket}/{key}")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(status, indent=2).encode("utf-8"),
            ContentType="application/json",
        )