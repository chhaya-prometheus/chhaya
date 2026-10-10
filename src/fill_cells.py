import geopandas as gpd
import h3
from shapely.geometry import Polygon

delhi = gpd.read_file("pipeline/data/raw/delhi.geojson")
geom = delhi.geometry.iloc[0]

hex_ids = h3.geo_to_cells(geom.__geo_interface__, res=8)
print(f"Hexagons: {len(hex_ids)}")

def hex_to_polygon(h):
    boundary = h3.cell_to_boundary(h)
    return Polygon([(lng, lat) for lat, lng in boundary])

hex_gdf = gpd.GeoDataFrame(
    {"h3_index": hex_ids},
    geometry=[hex_to_polygon(h) for h in hex_ids],
    crs="EPSG:4326"
)

print(hex_gdf.head())
print("Bounds:", hex_gdf.total_bounds)

hex_gdf.to_file("pipeline/data/derived/delhi_hex_res8.geojson", driver="GeoJSON")
hex_gdf.to_parquet("pipeline/data/derived/delhi_hex_res8.parquet")
print("Saved geojson + parquet")