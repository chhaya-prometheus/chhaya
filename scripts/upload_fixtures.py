"""
Upload city fixtures to S3.

Fixtures per city:
  - <slug>_boundary.geojson
  - <slug>_hex_res8.parquet
  - <slug>_wards.geojson       (if present)

S3 layout: fixtures/<slug>/<filename>

Idempotent — safe to rerun.

Usage:
    py -m scripts.upload_fixtures delhi
    py -m scripts.upload_fixtures --all
"""

import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from config import load_city, list_cities
from utils.logger import configLogger

log = configLogger(__file__)


def _s3_client(region: str):
    try:
        return boto3.client("s3", region_name=region)
    except NoCredentialsError:
        log.error("AWS credentials not found. Check .env or ~/.aws/credentials")
        sys.exit(1)


def _upload_one(s3, bucket: str, local_path: Path, s3_key: str) -> bool:
    if not local_path.exists():
        log.warning(f"  missing locally, skipping: {local_path.name}")
        return False

    size_kb = local_path.stat().st_size / 1024
    log.info(f"  uploading {local_path.name} ({size_kb:.1f} KB) → s3://{bucket}/{s3_key}")
    try:
        s3.upload_file(str(local_path), bucket, s3_key)
        return True
    except ClientError as e:
        log.error(f"  failed: {e}")
        return False


def upload_city(slug: str) -> bool:
    cfg = load_city(slug)
    bucket = cfg["aws"]["bucket"]
    region = cfg["aws"]["region"]
    prefix = cfg["fixtures"]["s3_prefix"].rstrip("/")
    cities_dir = Path(cfg["local"]["cities_dir"])

    log.info(f"[{slug}] uploading fixtures")
    s3 = _s3_client(region)

    uploads = [
        ("boundary", Path(cfg["raw"]["boundary"])),
        ("hex_grid", cities_dir / cfg["grid_fixture"]),
    ]
    # Add wards if declared
    wards_file = cfg.get("raw", {}).get("wards_file")
    if wards_file:
        uploads.append(("wards", Path(wards_file)))

    ok = 0
    for kind, path in uploads:
        s3_key = f"{prefix}/{slug}/{path.name}"
        if _upload_one(s3, bucket, path, s3_key):
            ok += 1

    log.info(f"[{slug}] done — {ok}/{len(uploads)} uploaded")
    return ok == len(uploads)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        log.error("usage: py -m scripts.upload_fixtures <slug> | --all")
        return 1

    slugs = list_cities() if args[0] == "--all" else args

    results = {slug: upload_city(slug) for slug in slugs}
    ok = sum(1 for v in results.values() if v)
    log.info(f"done — {ok}/{len(slugs)} cities uploaded")
    return 0 if ok == len(slugs) else 1


if __name__ == "__main__":
    sys.exit(main())