from pathlib import Path
import os
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[0]

CONFIG_DIR = PROJECT_ROOT / "config"
ENV = os.getenv("CHHAYA_ENV", "prod")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflicts."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_paths(cfg: dict) -> dict:
    """Resolve local dirs against PROJECT_ROOT so relative paths never break."""
    local = cfg.get("local", {})
    local["raw_dir"] = str(PROJECT_ROOT / local.get("raw_dir", "data/raw"))
    local["derived_dir"] = str(PROJECT_ROOT / local.get("derived_dir", "data/derived"))
    cfg["local"] = local

    fixtures = cfg.get("fixtures", {})
    fixtures["local_dir"] = local["raw_dir"]
    cfg["fixtures"] = fixtures

    return cfg


def load_config() -> dict:
    """Merge base.yaml + {ENV}.yaml, load .env, resolve paths. Cached."""
    if not hasattr(load_config, "_cache"):
        base = _read_yaml(CONFIG_DIR / "base.yaml")
        override = _read_yaml(CONFIG_DIR / f"{ENV}.yaml")
        merged = _deep_merge(base, override)

        load_dotenv(PROJECT_ROOT / ".env")

        merged = _resolve_paths(merged)
        merged["_env"] = ENV
        merged["_project_root"] = str(PROJECT_ROOT)
        load_config._cache = merged

    return load_config._cache