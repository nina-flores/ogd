import pandas as pd
import geopandas as gpd
import numpy as np
import rasterio
import rasterio.windows
from tqdm import tqdm
from shapely.validation import make_valid
from shapely.ops import unary_union
from shapely.geometry import shape
from rasterio.mask import mask
from exactextract import exact_extract

# Get best UTM projection per lat/lon
def get_best_utm_projection(lat, lon):
    zone_number = (lon + 180) // 6 + 1
    hemisphere = 326 if lat >= 0 else 327
    epsg_code = hemisphere * 100 + zone_number
    return f"EPSG:{int(epsg_code)}"

# Add UTM projection per geometry
def add_utm_projection(ch_shp: gpd.GeoDataFrame):
    ch_shp["centroid_lon"] = ch_shp.geometry.centroid.x
    ch_shp["centroid_lat"] = ch_shp.geometry.centroid.y
    ch_shp["utm_projection"] = ch_shp.apply(
        lambda row: get_best_utm_projection(row["centroid_lat"], row["centroid_lon"]),
        axis=1
    )
    return ch_shp[["ID_climate_hazard", "geometry", "utm_projection"]]

# Read and prepare spatial data
def prep_geographies(shp_path: str, geo_type: str):
    print(f"Reading {'hazard' if geo_type == 'hazard' else 'spatial unit'} geometries")
    shp_df = gpd.read_parquet(shp_path)

    geo_type_to_column = {"hazard": "ID_climate_hazard", "spatial_unit": "ID_unit"}
    new_column_name = geo_type_to_column.get(geo_type, "ID")

    # Rename first string column as ID
    string_col = shp_df.select_dtypes(include="object").columns[0]
    shp_df = shp_df.rename(columns={string_col: new_column_name})

    shp_df = shp_df[~shp_df.geometry.is_empty]  # Remove empty geometries
    shp_df["geometry"] = shp_df["geometry"].apply(make_valid)  # Fix invalid geometries
    shp_df = shp_df.to_crs("EPSG:4326")  # Ensure WGS84 CRS

    if geo_type == "hazard":
        shp_df = add_utm_projection(shp_df)
    
    return shp_df

# Efficiently add buffer distance
def add_buffer_distance_col(ch_df: gpd.GeoDataFrame):
    ch_df["buffer_dist"] = np.where(
        ch_df.geometry.area > 10000, 10000, 5000
    )  # Vectorized area-based buffer assignment
    return ch_df

# Buffer geometries while preserving per-row UTM projections
def add_buffered_geom_col(ch_df: gpd.GeoDataFrame):
    buffered_geoms = []
    for _, row in tqdm(ch_df.iterrows(), total=len(ch_df), desc="Buffering geometries"):
        geom_series = gpd.GeoSeries([row.geometry], crs="EPSG:4326").to_crs(row["utm_projection"])
        buffered_geom = geom_series.buffer(row["buffer_dist"]).to_crs("EPSG:4326").iloc[0]
        buffered_geoms.append(buffered_geom)

    ch_df["buffered_hazard"] = buffered_geoms
    return ch_df

# Efficiently combine overlapping geometries
def combine_overlapping_geometries(ch_df: gpd.GeoDataFrame, id_column: str):
    return ch_df.dissolve(by=id_column, aggfunc="sum").reset_index()

# Add bounding box column
def add_bounding_box_col(ch_df: gpd.GeoDataFrame):
    ch_df["bounding_box"] = ch_df.geometry.envelope
    return ch_df

# Optimize raster masking with `exact_extract`
def mask_raster_with_geoms(ch_df: gpd.GeoDataFrame, raster_path: str):
    with rasterio.open(raster_path) as src:
        ch_df = ch_df.to_crs(src.crs)
    ch_df["num_people_affected"] = exact_extract(raster_path, ch_df, "sum")
    return ch_df

# Prepare data pipeline
def prep_data(path_to_hazards: str, path_to_additional_geos: str = None):
    ch_shp = prep_geographies(path_to_hazards, geo_type="hazard")
    ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit") if path_to_additional_geos else None

    ch_shp = add_buffer_distance_col(ch_shp)
    ch_shp = add_buffered_geom_col(ch_shp)

    return (ch_shp, ad_geo) if ad_geo is not None else ch_shp

# Compute number of affected people
def find_num_people_affected_by_geo(path_to_hazards: str, path_to_additional_geos: str, raster_path: str):
    ch_shp, ad_geo = prep_data(path_to_hazards, path_to_additional_geos)

    # Find overlapping hazards
    ch_shp = combine_overlapping_geometries(ch_shp, id_column="ID_climate_hazard")
    unit_hazard_intersection = gpd.overlay(ch_shp, ad_geo, how="intersection")
    unit_hazard_intersection = add_bounding_box_col(unit_hazard_intersection)

    # Compute affected population
    num_af = mask_raster_with_geoms(unit_hazard_intersection, raster_path)
    return num_af[["ID_climate_hazard", "ID_unit", "num_people_affected"]]

# Compute population count per spatial unit
def find_number_of_people_residing_by_geo(path_to_additional_geos: str, raster_path: str):
    ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit")
    ad_geo = add_bounding_box_col(ad_geo)
    return mask_raster_with_geoms(ad_geo, raster_path)
