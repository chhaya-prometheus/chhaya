# src/sources/stubs.py
"""
Placeholder sources for columns not yet wired to real data.

Each stub produces deterministic fake values, seeded by (slug, h3_index).
Same slug + same hex = same value every run, on every machine.

Replace with real sources as they are built. Do not keep in production.

Set cfg["use_stubs"] = true in the city YAML or base YAML to enable.
"""

import hashlib
import random

import pandas as pd

from src.sources.base import SourceBase


def _seed(*parts: str) -> int:
    """Deterministic 32-bit seed from any set of strings."""
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _rng(slug: str, source: str, h3_index: str) -> random.Random:
    return random.Random(_seed(slug, source, h3_index))


class WorldPopStub(SourceBase):
    column_names = ["population"]

    def run(self) -> pd.DataFrame:
        grid = self._grid()
        slug = self.cfg["_slug"]

        rows = []
        for h in grid["h3_index"]:
            r = _rng(slug, "worldpop", h)
            # Plausible range for Delhi hexes: 0 to 25,000
            pop = int(r.triangular(0, 25000, 4000))
            rows.append({"h3_index": h, "population": pop})

        self.log.info(f"[{slug}] WorldPopStub — {len(rows)} rows")
        return pd.DataFrame(rows)


class OSMStub(SourceBase):
    column_names = ["school_count", "built_frac", "water_prox"]

    def run(self) -> pd.DataFrame:
        grid = self._grid()
        slug = self.cfg["_slug"]

        rows = []
        for h in grid["h3_index"]:
            r = _rng(slug, "osm", h)
            rows.append({
                "h3_index": h,
                "school_count": int(r.triangular(0, 6, 1)),
                "built_frac": round(r.triangular(0.05, 0.98, 0.55), 3),
                "water_prox": round(r.triangular(0, 1, 0.15), 3),
            })

        self.log.info(f"[{slug}] OSMStub — {len(rows)} rows")
        return pd.DataFrame(rows)


class LandsatStub(SourceBase):
    column_names = ["lst_c"]

    def run(self) -> pd.DataFrame:
        grid = self._grid()
        slug = self.cfg["_slug"]

        rows = []
        for h in grid["h3_index"]:
            r = _rng(slug, "landsat", h)
            # Plausible summer LST range: 32 to 48 C
            lst = round(r.triangular(32, 48, 40), 2)
            rows.append({"h3_index": h, "lst_c": lst})

        self.log.info(f"[{slug}] LandsatStub — {len(rows)} rows")
        return pd.DataFrame(rows)


class DustStub(SourceBase):
    column_names = ["dust_idx"]

    def run(self) -> pd.DataFrame:
        grid = self._grid()
        slug = self.cfg["_slug"]

        rows = []
        for h in grid["h3_index"]:
            r = _rng(slug, "dust", h)
            # 0 to 1, skewed toward low dust
            dust = round(r.betavariate(1.5, 4), 3)
            rows.append({"h3_index": h, "dust_idx": dust})

        self.log.info(f"[{slug}] DustStub — {len(rows)} rows")
        return pd.DataFrame(rows)