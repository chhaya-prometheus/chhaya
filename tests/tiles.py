"""
List every STAC item for Delhi's bbox in the date range, with how much
of the bbox it covers. Answers: 'what tiles exist, and why didn't the
picker choose the ones I need?'
"""

import argparse
from collections import defaultdict

from shapely.geometry import shape, box
from pystac_client import Client

from config import load_city, list_cities
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--city", default="delhi")
    p.add_argument("--max-items", type=int, default=500)
    args = p.parse_args(argv)

    cfg = load_city(args.city)
    s2 = cfg["sources"]["sentinel2"]
    bbox = box(*s2["bbox"])

    print(f"City: {args.city}")
    print(f"Bbox: {s2['bbox']}")
    print(f"Date range: {s2['date_range']}")
    print(f"Max cloud (config): {s2['max_cloud_cover']}%")
    print()

    catalog = Client.open(s2["stac_url"])
    search = catalog.search(
        collections=[s2["collection"]],
        bbox=s2["bbox"],
        datetime=s2["date_range"],
        max_items=args.max_items,
        # NOTE: no cloud filter — we want to see everything
    )
    items = list(search.item_collection())
    print(f"Total items (no cloud filter): {len(items)}\n")

    # Group by tile
    by_tile = defaultdict(list)
    for item in items:
        tile = item.properties.get("grid:code", "unknown")
        geom = shape(item.geometry)
        inter = geom.intersection(bbox)
        frac = inter.area / bbox.area if bbox.area else 0.0
        by_tile[tile].append({
            "id": item.id,
            "date": item.datetime.date(),
            "cloud": item.properties.get("eo:cloud_cover", 100.0),
            "bbox_frac": frac,
        })

    # Print sorted by best bbox coverage
    tiles_sorted = sorted(
        by_tile.items(),
        key=lambda kv: -max(x["bbox_frac"] for x in kv[1]),
    )

    print(f"{'tile':<12} {'date':<12} {'cloud':>6} {'bbox%':>7}  id")
    print("-" * 80)
    for tile, entries in tiles_sorted:
        for e in sorted(entries, key=lambda x: -x["bbox_frac"]):
            flag = ""
            if e["cloud"] > s2["max_cloud_cover"]:
                flag = "  [cloud>max]"
            print(
                f"{tile:<12} {str(e['date']):<12} "
                f"{e['cloud']:>5.1f}% {e['bbox_frac']:>6.1%}  "
                f"{e['id']}{flag}"
            )

    # Union coverage assuming we could use EVERY tile
    print("\n--- Theoretical max coverage ---")
    tiles_in_bbox = [
        tile for tile, entries in by_tile.items()
        if max(x["bbox_frac"] for x in entries) > 0.01
    ]
    print(f"Tiles intersecting bbox (>1%): {len(tiles_in_bbox)}")
    print(f"Tiles: {sorted(tiles_in_bbox)}")
    # scripts/diagnose_tiles.py, add at the end:


    bbox_poly = shapely_box(*s2["bbox"])

    # Pick the BEST scene per tile (highest bbox overlap, then lowest cloud)
    best_per_tile = {}
    for item in items:
        tile = item.properties.get("grid:code")
        geom = shape(item.geometry)
        frac = geom.intersection(bbox_poly).area / bbox_poly.area
        cloud = item.properties.get("eo:cloud_cover", 100)
        prev = best_per_tile.get(tile)
        if prev is None or (frac > prev["frac"]) or (
            abs(frac - prev["frac"]) < 0.01 and cloud < prev["cloud"]
        ):
            best_per_tile[tile] = {"geom": geom, "frac": frac, "cloud": cloud, "id": item.id}

    print("\n--- Best scene per tile ---")
    for tile, b in sorted(best_per_tile.items()):
        print(f"  {tile}: {b['id']}  frac={b['frac']:.1%}  cloud={b['cloud']:.1f}%")

    # Union of all best-per-tile footprints
    union_geom = unary_union([b["geom"] for b in best_per_tile.values()])
    union_frac = union_geom.intersection(bbox_poly).area / bbox_poly.area
    print(f"\nUnion of ALL tile footprints covers bbox: {union_frac:.1%}")

    # Union of just the two big tiles
    big_tiles = sorted(best_per_tile.items(), key=lambda kv: -kv[1]["frac"])[:2]
    big_union = unary_union([b["geom"] for _, b in big_tiles])
    big_frac = big_union.intersection(bbox_poly).area / bbox_poly.area
    print(f"Union of top-2 tiles ({', '.join(t for t,_ in big_tiles)}) covers: {big_frac:.1%}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))

    