"""
One-time (rerunnable) uploader for fixture files to S3.

Fixtures = slowly-changing files every environment needs:
  - city boundary geojson
  - H3 hex grid parquet

Run once per city. Safe to rerun — overwrites existing objects.

Usage:
    py -m scripts.upload_fixtures
"""

import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from config import load_config
from utils.logger import configLogger

log = configLogger(__file__)
cfg = load_config()


# Fixtures declared in base.yaml under fixtures.files
# Format: { logical_name: s3_key }
FIXTURE_FILES = cfg["fixtures"]["files"]
BUCKET = cfg["aws"]["bucket"]
REGION = cfg["aws"]["region"]
LOCAL_DIR = Path(cfg["fixtures"]["local_dir"])


def _s3_client():
    try:
        return boto3.client("s3", region_name=REGION)
    except NoCredentialsError:
        log.error("AWS credentials not found. Check .env or ~/.aws/credentials")
        sys.exit(1)


def _local_path_for(s3_key: str) -> Path:
    """Fixtures are stored flat locally; take just the filename from the S3 key."""
    return LOCAL_DIR / Path(s3_key).name


def _upload_one(s3, local_path: Path, s3_key: str) -> bool:
    if not local_path.exists():
        log.warning(f"missing locally, skipping: {local_path}")
        return False

    size_kb = local_path.stat().st_size / 1024
    log.info(f"uploading {local_path.name} ({size_kb:.1f} KB) → s3://{BUCKET}/{s3_key}")

    try:
        s3.upload_file(str(local_path), BUCKET, s3_key)
        log.info(f"  ✓ {s3_key}")
        return True
    except ClientError as e:
        log.error(f"  ✗ failed: {e}")
        return False


def main() -> int:
    log.info(f"fixture upload starting — bucket={BUCKET}, region={REGION}")
    log.info(f"local fixture dir: {LOCAL_DIR}")

    s3 = _s3_client()
    succeeded = 0
    failed = 0

    for name, s3_key in FIXTURE_FILES.items():
        local_path = _local_path_for(s3_key)
        if _upload_one(s3, local_path, s3_key):
            succeeded += 1
        else:
            failed += 1

    log.info(f"done — {succeeded} uploaded, {failed} skipped/failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())