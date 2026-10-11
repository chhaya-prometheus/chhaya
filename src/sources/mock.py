"""
Synthetic data source for demo cities.

Produces spatially-coherent, deterministic, plausible values for every
non-geometry column the pipeline expects:

    ndvi, lst_c, population, built_frac, water_prox, dust_idx, school_count

Determinism: seeded by (slug, h3_index). Same slug + hex = same values
on every machine, every run.

Spatial coherence: uses N gaussian "blobs" placed by seed over the
city's bbox — a dense core, green pockets, a dusty/industrial zone.
Values are derived from distance to these blobs.

Marked as demo. Never presented as real.
"""

import hashlib
import math
import random

import pandas as pd
from shapely.geometry import Point

from src.sources.base import SourceBase


BLOBS_PER_CITY = 5   # dense core + 2 green + 1 dust + 1 extra
CELL_AREA_KM2 = 0.737  # H3 res-8 average


def _seed(*parts: str) -> int:
    return int(hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:8], 16)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class MockSource(SourceBase):
    column_names = [
        "ndvi", "lst_c", "population", "built_frac",
        "water_prox", "dust_idx", "school_count",
    ]

    def run(self) -> pd.DataFrame:
        grid = self._grid()
        slug = self.cfg["_slug"]

        # Determine city bbox in lon/lat
        b = grid.total_bounds   # [minx, miny, maxx, maxy]
        lon_min, lat_min, lon_max, lat_max = b
        lon_span = lon_max - lon_min
        lat_span = lat_max - lat_min

        # Place blob centers deterministically
        rng_blobs = random.Random(_seed(slug, "blobs"))
        blobs = self._make_blobs(rng_blobs, lon_min, lat_min, lon_span, lat_span)

        rows = []
        for h in grid["h3_index"]:
            # Centroid of this hex via geometry
            geom = grid.loc[grid["h3_index"] == h, "geometry"].iloc[0]
            c = geom.centroid
            rows.append(self._row_for_hex(slug, h, c.x, c.y, blobs))

        df = pd.DataFrame(rows)
        self.log.info(
            f"[{slug}] MockSource — {len(df)} rows, "
            f"ndvi mean={df['ndvi'].mean():.3f}, "
            f"lst mean={df['lst_c'].mean():.1f}, "
            f"pop mean={df['population'].mean():.0f}"
        )
        return df

    # ---------- internals ----------

    def _make_blobs(self, rng, lon_min, lat_min, lon_span, lat_span):
        """
        Create 5 blobs: 1 dense (population + built), 2 green (ndvi),
        1 dusty, 1 extra dense/green at random.
        Each blob has (lon, lat, radius_frac, type, strength).
        """
        types = ["dense", "green", "green", "dust", "extra"]
        blobs = []
        for t in types:
            # Position anywhere inside the bbox with some margin
            lon = lon_min + rng.uniform(0.1, 0.9) * lon_span
            lat = lat_min + rng.uniform(0.1, 0.9) * lat_span
            radius = rng.uniform(0.15, 0.35)  # fraction of max span
            strength = rng.uniform(0.7, 1.0)
            if t == "extra":
                t = rng.choice(["dense", "green"])
            blobs.append({"lon": lon, "lat": lat, "radius": radius,
                          "type": t, "strength": strength})
        return blobs

    def _blob_influence(self, x, y, blob, lon_span, lat_span) -> float:
        """Gaussian falloff: 1 at center, ~0 far away."""
        dx = (x - blob["lon"]) / (blob["radius"] * lon_span + 1e-9)
        dy = (y - blob["lat"]) / (blob["radius"] * lat_span + 1e-9)
        d2 = dx * dx + dy * dy
        return blob["strength"] * math.exp(-d2)

    def _row_for_hex(self, slug, h, x, y, blobs) -> dict:
        # Need spans inside _row_for_hex; recompute once per call is fine
        # but wasteful. We cache on the instance for this run.
        if not hasattr(self, "_spans"):
            b = self._grid().total_bounds
            self._spans = (b[2] - b[0], b[3] - b[1])
        lon_span, lat_span = self._spans

        dense = 0.0
        green = 0.0
        dust = 0.0
        for blob in blobs:
            infl = self._blob_influence(x, y, blob, lon_span, lat_span)
            if blob["type"] == "dense":
                dense = max(dense, infl)
            elif blob["type"] == "green":
                green = max(green, infl)
            elif blob["type"] == "dust":
                dust = max(dust, infl)

        r = random.Random(_seed(slug, h))

        # ndvi
        ndvi = _clamp(0.10 + 0.28 * green - 0.05 * dense + r.gauss(0, 0.03), 0.02, 0.62)
        # built_frac
        built = _clamp(0.55 + 0.30 * dense - 0.35 * green + r.gauss(0, 0.05), 0.08, 0.97)
        # lst_c
        lst = _clamp(43.5 - 22 * (ndvi - 0.1) + 4.5 * (built - 0.5) + r.gauss(0, 0.5), 28, 52)
        # population density
        density = 1500 + 36500 * dense
        pop = int(density * CELL_AREA_KM2 * r.uniform(0.7, 1.3))
        # dust_idx
        dust_val = _clamp(0.15 + 0.7 * dust + r.betavariate(1.5, 4) * 0.3, 0, 1)
        # water_prox
        water = _clamp(r.betavariate(1.2, 5.0), 0, 1)
        # school_count
        schools = max(0, round(pop / 9000 + r.gauss(0, 0.5)))

        return {
            "h3_index": h,
            "ndvi": round(ndvi, 3),
            "lst_c": round(lst, 2),
            "population": pop,
            "built_frac": round(built, 3),
            "water_prox": round(water, 3),
            "dust_idx": round(dust_val, 3),
            "school_count": int(schools),
        }