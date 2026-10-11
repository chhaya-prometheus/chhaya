from pathlib import Path
import os
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[0]

load_dotenv(PROJECT_ROOT / ".env")

CONFIG_DIR = PROJECT_ROOT / "config"
CITIES_DIR = CONFIG_DIR / "cities"

ENV = os.getenv("CHHAYA_ENV")


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

def list_cities() -> list[str]:
    """Return slugs of all cities with a config file."""
    if not CITIES_DIR.exists():
        return []
    return sorted(
        p.stem for p in CITIES_DIR.glob("*.yaml")
    )


def _resolve_city_paths(city: dict, base: dict) -> dict:
    """Resolve raw.* paths against PROJECT_ROOT and grid_fixture against cities_dir."""
    cities_dir = PROJECT_ROOT / base["local"]["cities_dir"]

    raw = city.get("raw", {})
    for key in ("boundary", "wards_file"):
        if key in raw and not Path(raw[key]).is_absolute():
            raw[key] = str(PROJECT_ROOT / raw[key])
    city["raw"] = raw

    fixture_name = city.get("grid_fixture")
    if fixture_name:
        city["grid_fixture_path"] = str(cities_dir / fixture_name)

    city["_cities_dir"] = str(cities_dir)
    return city


def load_city(slug: str) -> dict:
    """Load one city's config, merged with base.yaml."""
    path = CITIES_DIR / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No city config for '{slug}'. Expected {path}"
        )

    base = load_config()
    city = _read_yaml(path)

    # Merge: base under city, city wins on conflicts
    merged = _deep_merge(base, city)

    # Merge sentinel2: base's url/collection/etc + city's bbox/epsg
    city_s2 = city.get("sentinel2", {})
    if city_s2:
        merged["sources"]["sentinel2"] = _deep_merge(
            merged["sources"]["sentinel2"], city_s2
        )

    merged = _resolve_city_paths(merged, base)
    merged["_slug"] = slug
    return merged