# Known Limitations & Engineering Debt

**Pipeline:** Sentinel-2 → NDVI → H3 (Delhi)
**Last validated:** 2026-10-18
**Validation:** `py -m tests.validate` + `py -m tests.spot_check`
**Date range validated:** 2026-05-01 / 2026-06-30

Only real limitations. Each item has impact, severity, and mitigation.

---

## 🔴 High — users must know

### G1: ~20% of pixels are NaN in the NDVI raster

`_compute_ndvi` rejects pixels where `|nir + red| ≤ 1e-6` (denominator guard). On Delhi this drops **~20%** of pixels — mostly dark water, deep shadow, and pixels sitting near the BOA_ADD_OFFSET boundary.

**Why it's correct:** Sentinel-2 L2A applies `BOA_ADD_OFFSET = -1000`, so pixels with raw DN near 1000 become reflectance near 0. The ratio `(nir-red)/(nir+red)` blows up when `nir + red ≈ 0`. The guard rejects those — they're physically meaningless.

**Impact:** Per-pixel reads over water/shadow return NaN. **Hex-level zonal coverage is still 100%** because hexes aggregate multiple pixels.

**Mitigation:** Downstream consumers should use **hex-level NDVI, not per-pixel**. Documented.

---

### G2: MGRS tile gap around Sanjay Van (~1% of AOI)

~1% of Delhi's bbox (concentrated around Sanjay Van, South Delhi) has **no data** from any of the 4 intersecting tiles. Pixels there are NaN, silently.

**Why:** Delhi sits at the corner where `RFM`, `RGM`, `RFN`, `RGN` meet. Their union covers ~99% of the bbox, leaving a strip at the tile-join. **No config change fixes this** — it's Sentinel-2 tiling geometry.

**Impact:** Hexes overlapping the gap still get values via zonal aggregation. A hex entirely inside the gap would be NaN.

**Mitigation:** Accept. For other cities, run `tests.diagnose_tiles` first — if a gap falls in a critical area, supplement with Landsat.

---

### G3: Composite mixes dates across tiles

`_pick_scenes` takes the **first clean scene per tile**, not the closest-in-date. Delhi's current composite mixes **2026-05-28** and **2026-06-17** — a 20-day span.

**Impact:** Small (~0.02–0.05 NDVI bias) for pre-monsoon Delhi. **Meaningful for monsoon or post-harvest date ranges** where vegetation changes rapidly.

**Mitigation:** Accept for May–June. If changing date range, update sort key to `(bbox_overlap, cloud, |datetime - anchor|)` in `_pick_scenes`.

---

## 🟡 Medium — silent risks

### G4: Band names coupled to STAC asset aliases

`_load_bands` asserts `{"red", "nir"} ⊆ cfg["bands"]` and does `stack.sel(band="red")`. Other STAC providers may use `"B04"`/`"B08"`.

**Impact:** `KeyError` after a download attempt if the provider changes aliases.

**Mitigation:** Already fails fast via the assert. Update `cfg["bands"]` in `base.yaml` when switching providers.

---

### G5: Cache key ignores config parameters

`_cache_path` uses `md5(sorted scene IDs)` only. Changing `epsg`, `resolution_m`, or `bands` while keeping the same scenes → **cache hit loads stale composite**.

**Impact:** Silent stale data. Already bit us once (mask fix didn't take effect until cache deleted manually).

**Mitigation:** Delete cache when changing processing params:

```powershell
Remove-Item data/raw/sentinel2/composite_*_ndvi.tif
```

Or hash config into the cache key:

```python
def _cache_path(self, scenes: list) -> Path:
    cache_dir = Path(self.cfg["local"]["raw_dir"]) / "sentinel2"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cfg = self._cfg()
    key_src = (
        "|".join(sorted(s.id for s in scenes))
        + f"|epsg={cfg['epsg']}"
        + f"|res={cfg['resolution_m']}"
        + f"|bands={','.join(sorted(cfg['bands']))}"
    )
    key = hashlib.md5(key_src.encode()).hexdigest()[:10]
    return cache_dir / f"composite_{key}_ndvi.tif"
```

---

### G6: BOA_ADD_OFFSET assumed applied by provider

`red min = -0.1` in the diagnostic confirms the offset **is applied**, but **we don't apply it** — we trust the COG. CDSE bakes it in; Earth Search / AWS requires user application.

**Impact:** If switching providers, raw `[0, 10000]` vs scaled `[-0.1, 1.0]` changes NDVI behavior silently.

**Mitigation:** Re-run `tests.validate` after switching providers. Check `red min` in the diagnostic. If raw, apply the offset in `_load_bands`.

---

## 🟢 Low — harmless now, revisit later

### G7: `composite.scenes` caps tile count

For Delhi, `scenes=4` matches exactly the 4 intersecting tiles. For a city spanning 6 tiles, `scenes=4` would silently miss 2.

**Mitigation:** Run `tests.diagnose_tiles` first. Set `scenes` ≥ intersecting tile count.

---

### G8: `xr.open_dataarray` unreliable for single-band GeoTIFFs

Reading cached NDVI with `xr.open_dataarray` sometimes returns a 1-element array instead of `(y, x)`. Broke `spot_check` once.

**Mitigation:** Use `rioxarray.open_rasterio(cache).squeeze(drop=True)` everywhere a cached raster is read. `tests.spot_check` already does this; `run()` in `sentinel2.py` should too.

---

### G9: Two compute() passes on red + nir

`stack.sel(band="red").compute()` and `stack.sel(band="nir").compute()` are separate Dask passes. **Not a real bottleneck** — different COGs on the server. Left as-is.

---

## ⚪ Info — not bugs, just data properties

- **I1:** `red min = -0.1` in the diagnostic is expected from BOA_ADD_OFFSET. Not a bug.
- **I2:** ~2 independent observations per pixel (because `RFM`/`RGM` overlap heavily). Median of 2 handles one cloudy outlier, not two. Fine for zero-cloud picks; bump `scenes` if cloudy selections appear.

---

## Summary

| ID | Severity | Issue | Status |
|---|---|---|---|
| G1 | 🔴 | 20% pixel NaN from denominator guard | Handled by zonal |
| G2 | 🔴 | Sanjay Van tile gap (~1%) | Documented; Landsat if critical |
| G3 | 🔴 | Mixed-date composite (May + June) | OK now; fix for other ranges |
| G4 | 🟡 | Band names tied to STAC aliases | Fails fast |
| G5 | 🟡 | Cache key ignores config | Delete cache on changes |
| G6 | 🟡 | Provider offset assumed | Re-verify per provider |
| G7 | 🟢 | `scenes` cap may miss tiles | Diagnose first |
| G8 | 🟢 | `open_dataarray` on 1-band GeoTIFF | Use `open_rasterio` |
| G9 | 🟢 | Two compute passes | No action |

---

## Pre-flight for new city / date range

```powershell
py -m tests.diagnose_tiles --city <slug>      # check tile coverage
py -m tests.validate --city <slug> --no-cache # expect 20/20
py -m tests.spot_check --city <slug>          # expect 3+/4
```

If `diagnose_tiles` shows tile union < 95% of bbox, you have a gap — fix bbox or accept.

---

## Revisit if...

| Trigger | Re-read |
|---|---|
| New city | G2, G7 |
| New date range | G3 |
| New STAC provider | G4, G6 |
| Change `epsg` / `resolution_m` | G5 |
| New downstream consumer | G1, G2 |

---

*Last updated: 2026-10-18*