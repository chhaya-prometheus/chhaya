"""
S3 read/write for pipeline artifacts.

Only this module touches S3. Everything else writes to local disk.
Handles the two-tier merge: new source columns overwrite old values,
failed sources keep their previous values.

One S3Exporter instance per city. The instance reads cfg["_slug"] and
builds all S3 keys from it: features/<slug>_cells.parquet, etc.
"""

import io
import json
from pathlib import Path

import boto3
import geopandas as gpd
import pandas as pd
from botocore.exceptions import ClientError


class S3Exporter:
    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.slug = cfg["_slug"]
        self.bucket = cfg["aws"]["bucket"]
        self.region = cfg["aws"]["region"]
        self.prefix = cfg["artifacts"]["s3_prefix"].rstrip("/")
        self.client = boto3.client("s3", region_name=self.region)

    # ---------- key builders ----------

    def _key_cells_parquet(self) -> str:
        return f"{self.prefix}/{self.slug}_cells.parquet"

    def _key_cells_geojson(self) -> str:
        return f"{self.prefix}/{self.slug}_cells.geojson"

    def _key_status(self) -> str:
        return f"{self.prefix}/{self.slug}_status.json"

    def _key_wards_geojson(self) -> str:
        return f"{self.prefix}/{self.slug}_wards.geojson"

    def _key_ward_stats(self) -> str:
        return f"{self.prefix}/{self.slug}_ward_stats.json"

    def _key_cities_index(self) -> str:
        return f"{self.prefix}/cities.json"

    # ---------- read ----------

    def load_existing_cells(self) -> gpd.GeoDataFrame | None:
        """Fetch previous <slug>_cells.parquet from S3, or None if first run."""
        key = self._key_cells_parquet()
        try:
            self.log.info(f"[{self.slug}] reading s3://{self.bucket}/{key}")
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            gdf = gpd.read_parquet(io.BytesIO(obj["Body"].read()))
            self.log.info(f"[{self.slug}] loaded existing cells — {len(gdf)} rows")
            return gdf
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                self.log.info(f"[{self.slug}] no existing cells — first run")
                return None
            raise

    def load_cities_index(self) -> list[dict]:
        """Read existing features/cities.json, or [] if missing."""
        key = self._key_cities_index()
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            data = json.loads(obj["Body"].read().decode("utf-8"))
            return data if isinstance(data, list) else []
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return []
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

        merged = new.copy()

        for col in old.columns:
            if col in ("h3_index", "geometry"):
                continue
            if col in ok_columns:
                continue
            if col not in merged.columns:
                merged[col] = None

            old_indexed = old.set_index("h3_index")[col]
            merged_indexed = merged.set_index("h3_index")
            merged_indexed[col] = merged_indexed[col].fillna(old_indexed)
            merged = merged_indexed.reset_index()
            merged = gpd.GeoDataFrame(merged, geometry="geometry", crs=new.crs)

        return merged

    # ---------- write ----------

    def upload_local(self, local_path: Path, s3_key: str) -> None:
        size_kb = local_path.stat().st_size / 1024
        self.log.info(
            f"[{self.slug}] uploading {local_path.name} ({size_kb:.1f} KB) "
            f"→ s3://{self.bucket}/{s3_key}"
        )
        try:
            self.client.upload_file(str(local_path), self.bucket, s3_key)
            self.log.info(f"[{self.slug}]   ✓ {s3_key}")
        except ClientError as e:
            self.log.error(f"[{self.slug}]   ✗ {e}")
            raise

    def upload_bytes(self, data: bytes, s3_key: str, content_type: str) -> None:
        self.log.info(f"[{self.slug}] uploading {s3_key} ({len(data)/1024:.1f} KB)")
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=s3_key,
                Body=data, ContentType=content_type,
            )
            self.log.info(f"[{self.slug}]   ✓ {s3_key}")
        except ClientError as e:
            self.log.error(f"[{self.slug}]   ✗ {e}")
            raise

    def write_status(self, status: dict) -> None:
        key = self._key_status()
        self.upload_bytes(
            json.dumps(status, indent=2).encode("utf-8"),
            key,
            "application/json",
        )

    def write_wards_geojson(self, local_path: Path) -> None:
        self.upload_local(local_path, self._key_wards_geojson())

    def write_ward_stats(self, stats: list[dict]) -> None:
        self.upload_bytes(
            json.dumps(stats, indent=2).encode("utf-8"),
            self._key_ward_stats(),
            "application/json",
        )

    # ---------- cities index (shared, not slug-scoped) ----------

    def write_cities_index(self, cities: list[dict]) -> None:
        key = self._key_cities_index()
        self.log.info(f"writing cities index → s3://{self.bucket}/{key}")
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key,
                Body=json.dumps(cities, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
            self.log.info(f"  ✓ {key} ({len(cities)} cities)")
        except ClientError as e:
            self.log.error(f"  ✗ {e}")
            raise