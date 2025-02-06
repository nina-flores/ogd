import pandas as pd
import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import rasterio
import rasterio.windows
from tqdm import tqdm  # Corrected import statement
from rasterio.mask import mask
from shapely.validation import make_valid
from shapely.ops import unary_union
from shapely import wkt
from shapely.geometry import shape
from rasterio.features import geometry_mask
from rasterio.features import geometry_mask, rasterize
import geopandas as gpd
from exactextract import exact_extract


# take a lat lon pair and return the best UTM projection for that lat lon
def get_best_utm_projection(lat, lon):
    zone_number = (lon + 180) // 6 + 1
    hemisphere = 326 if lat >= 0 else 327
    epsg_code = hemisphere * 100 + zone_number
    return f"EPSG:{int(epsg_code)}"


# add UTM projection column to a geodataframe initially only containing
# climate hazard IDs and a geometry column. this column will contain the best
# UTM projection for the centroid of each geometry
# -----------------------------------------------------------------------------
# will call this before running any buffering in final functions,
# as buffering is done in meters and we want to make sure we're using the
# best UTM projection for the data to minimize distortion.
# note that data is in WGS84
# this is a geographic crs so should get correct lat/lon
# centroid calculation may be slightly off if the data is in a projected crs
# but we're trading that off to get correct lat/lon
# also note centroid for points is just the point itself
def add_utm_projection(ch_shp: gpd.GeoDataFrame):

    # get lat and lon
    ch_shp["centroid_lon"] = ch_shp.centroid.x
    ch_shp["centroid_lat"] = ch_shp.centroid.y

    # get projection for each hazard
    ch_shp["utm_projection"] = ch_shp.apply(
        lambda row: get_best_utm_projection(
            lat=row["centroid_lat"], lon=row["centroid_lon"]
        ),
        axis=1,
    )

    # select id, geometry, and utm projection
    ch_shp = ch_shp[["ID_climate_hazard", "geometry", "utm_projection"]]

    return ch_shp


# read in a climate hazard shapefile or spatial unit shapefile (counties,
# zcta, etc) in parquet format that contains a string column with the geom ID
# and a geography column, but nothing else. this function renames the ID cols
# consistently, makes geoms valid, adds a column indicating the best UTM
# projection, and reprojects to WGS84
# hazard ID should be a string and geometry should be the geographies column and
# must be named "geometry"
# --------------------------------------------------------------------------
# need some checks and to throw errors if the dataframe cols are not the
# correct type or if there are more than two cols
# - more than 2
# - wrong types
# - no geometry col
# include these in the validations, and have those be part of a validation
# function that runs first
def prep_geographies(shp_path: str, geo_type: str):
    # print message
    if geo_type == "hazard":
        print(
            f"Reading data and finding best UTM projection for hazard geometries (1/6)"
        )
    elif geo_type == "spatial_unit":
        print(f"Reading spatial unit geometries (1/6)")

    # read in parquet
    shp_df = gpd.read_parquet(shp_path)

    # rename the appropriate column based on the geo_type
    geo_type_to_column_name = {"hazard": "ID_climate_hazard", "spatial_unit": "ID_unit"}
    new_column_name = geo_type_to_column_name.get(geo_type)

    # want to have different names for hazards vs geographies
    string_col = shp_df.select_dtypes(include="object").columns[0]
    shp_df = shp_df.rename(columns={string_col: new_column_name})

    # remove missing geoms
    shp_df = shp_df[~shp_df["geometry"].is_empty]

    # make valid geoms, esp important for hazards
    shp_df["geometry"] = shp_df["geometry"].apply(make_valid)

    # reproject to WGS84
    if shp_df.crs != "EPSG:4326":
        shp_df = shp_df.to_crs("EPSG:4326")

    # if hazard, add best projection
    if geo_type == "hazard":
        shp_df = add_utm_projection(shp_df)

    return shp_df


# takes in a geodataframe containing climate hazards, and mutates it to add b
# a buffer distance column.
# there are two options for buffer distance, assigned based
# on whether the hazard area is larger than an area threshold, in square meters
def add_buffer_distance_col(
    ch_df: gpd.GeoDataFrame,
    buffer_dist_large: int = 10000,  # 10 km
    buffer_dist_small: int = 5000,  # 5 km
    area_thresh_for_large_buffer: int = 10000,  # 10 km^2
):
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Adding buffer distance (2/6)"
    ):
        hazard_geometry = row["geometry"]

        # decide on buffer distance in m based on area threshold in m^2
        if hazard_geometry.area > area_thresh_for_large_buffer:
            buffer_dist = buffer_dist_large
        else:
            buffer_dist = buffer_dist_small

        # save buffer dist
        ch_df.at[index, "buffer_dist"] = buffer_dist

    return ch_df


# mutate a dataframe containing climate hazards: buffer each climate hazard
# geometry, based on previously ided distances, and add new col containing a
# new buffered hazard geometry
def add_buffered_geom_col(ch_df: gpd.GeoDataFrame):
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Buffering hazard geometries (3/6)"
    ):
        best_utm = row["utm_projection"]
        hazard_geom = row["geometry"]

        # create geoseries in best projection
        geom_series = gpd.GeoSeries([hazard_geom], crs=ch_df.crs)
        geom_series_utm = geom_series.to_crs(best_utm)

        # buffer distance is in meters
        buffer_dist = row["buffer_dist"]
        buffered_hazard_geometry = geom_series_utm.buffer(buffer_dist).iloc[0]
        # back to OG
        buffered_hazard_geometry = (
            gpd.GeoSeries([buffered_hazard_geometry], crs=best_utm)
            .to_crs(ch_df.crs)
            .iloc[0]
        )
        # add
        ch_df.at[index, "buffered_hazard"] = buffered_hazard_geometry

    return ch_df


# prep data: this function takes in path names to climate hazards and additional
# geographies, and calls the above helpers to read in data, find best UTM crs
# for each climate hazard, add buffer distances, and buffer hte hazards.
# it returns a geodataframe with the hazard IDs, the original hazard geometry,
# best UTM projection, buffer distance, and buffered hazard geometry. if there
# are additional geos, it returns a tuple of the above plus a dataframe
# containing the additional geo IDs and geometries.
def prep_data(
    path_to_hazards: str,
    path_to_additional_geos: str = None,
    buffer_dist_large: int = 10000,
    buffer_dist_small: int = 5000,
    area_thresh_for_large_buffer: int = 10000,
):

    # prep both geographies
    ch_shp = prep_geographies(path_to_hazards, geo_type="hazard")
    # if additional_geos isn't None, do this step too
    if path_to_additional_geos:
        ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit")

    # add buffer distance to climate hazards - small buffer if hazard is
    # smaller than area threshold, large buffer if larger
    ch_shp = add_buffer_distance_col(
        ch_shp,
        buffer_dist_large=buffer_dist_large,
        buffer_dist_small=buffer_dist_small,
        area_thresh_for_large_buffer=area_thresh_for_large_buffer,
    )

    # add buffered hazard geometry col to climate hazards
    ch_shp = add_buffered_geom_col(ch_shp)

    if path_to_additional_geos:
        return ch_shp, ad_geo
    else:
        return ch_shp


# take a geodataframe of geometries (named 'geometry' column) and their IDs,
# and if two geometries overlap, combine them via unary union into one geometry.
# if more than two overlap, combine them all via unary union into one geometry.
# the IDs of any geometries that are combined are concatonated with underscores
# in between. but if geometries do not overlap they are untouched and IDs are
# untouched. return a dataframe of new unioned geometries and their IDs
# IDs will have the same name as the input ID column, geom called 'geometry'
# ------------------------------------------------------------------------
# this function is hacky. if anyone has ideas to improve please!
# also there is some complexity. in the test dataset sometimes there is one
# fire that is two geometries combined. in that case, this function first splits
# those up into two geometries both with the same ID, and then finds geometries
# that overlap with those invididually.
# ------------------------------------------------------------------------
# this means that the result of 'find people affected' will be a list of
# combined geometries (fires that overlapped in the dataset) and those affected.
# if you want to find people affected by
# a specific fire, you might need to sum over all the geometries that are part
# of that fire. for geos, for each zcta (or similar) you'll get people affected
# by fires that overlapped and overlapped the zcta. there might be more than
# one group of fires/hazards
def combine_overlapping_geometries(ch_df: gpd.GeoDataFrame, id_column: str):
    # Create a spatial index for the geometries
    spatial_index = ch_df.sindex

    # Initialize a list to store the results
    results = []
    processed_indices = set()

    # Process each geometry
    for idx, row in ch_df.iterrows():
        if idx in processed_indices:
            continue

        geom = row['geometry']
        geom_id = row[id_column]

        # Find all geometries that intersect with the current geometry
        possible_matches_index = list(spatial_index.intersection(geom.bounds))
        possible_matches = ch_df.iloc[possible_matches_index]

        # Filter to only those that actually intersect
        precise_matches = possible_matches[possible_matches.intersects(geom)]

        # If there are overlaps, combine them
        if len(precise_matches) > 1:
            combined_geom = unary_union(precise_matches['geometry'])
            combined_ids = '___'.join(precise_matches[id_column].unique())
            results.append({id_column: combined_ids, 'geometry': combined_geom})

            # Mark the processed geometries
            processed_indices.update(precise_matches.index)
        else:
            results.append({id_column: geom_id, 'geometry': geom})

    # Convert results to a GeoDataFrame
    combined_geoms = gpd.GeoDataFrame(results, crs=ch_df.crs)

    return combined_geoms


# mutate a dataframe containing climate hazards: create envelop buffer for
# the geodataframe's geometries, and add these as a col to the ch dataframe.
# doing this to make it easy to load raster for pop only in the bounding box
# of the hazard for ~*swiftness*~. arg to add a buffer to the bounding box if
# going to use to calculate pop dens
def add_bounding_box_col(
    ch_df: gpd.GeoDataFrame, target_col: str, buffer_buffer: int = 0
):
    # if we're not doing pop dens the bounding box should be no larger than
    # the buffered geography. for pop dens need room for radius of convolution
    # kernel
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Adding bounding box (5/6)"
    ):
        target_col_geom = row[target_col]
        bounding_box = target_col_geom.envelope.buffer(buffer_buffer)
        ch_df.at[index, "bounding_box"] = bounding_box

    return ch_df


# this function does the fast raster read in piece of the larger functions
# it takes in a dataframe with buffered hazard geometries and bounding boxes,
# and it opens and masks a raster to find how many people are in those
# geographies.
def mask_raster_with_geoms(ch_df: gpd.GeoDataFrame, raster_path: str):
    # find num of people in geoms
    for index, row in tqdm(
        ch_df.iterrows(), total=len(ch_df), desc="Masking raster (6/6)"
    ):
        bounding_box = row["bounding_box"]
        buffered_hazard_geometry = row["geometry"]

        # load raster data only in bounding box, need bounding box to mol
        mol_crs = "ESRI:54009"
        bbox_transformed = (
            gpd.GeoSeries([bounding_box], crs="EPSG:4326").to_crs(mol_crs).iloc[0]
        )

        # open the raster file
        with rasterio.open(raster_path) as src:
            # read raster data within the bounding box
            window = src.window(*bbox_transformed.bounds)
            pop_data = src.read(1, window=window)
            pop_transform = src.window_transform(window)

            # mask w buffered hazard so we can sum over hazard area
            buffered_hazard_geometry = gpd.GeoSeries(
                [buffered_hazard_geometry], crs="EPSG:4326"
            ).to_crs(src.crs)
            out_image, out_transform = mask(
                src,
                buffered_hazard_geometry.geometry,
                crop=True,
                filled=True,
                nodata=0,
                all_touched=True,
            )

            # sum over hazard area and save
            pop_sum = np.sum(out_image)
            ch_df.at[index, "num_people_affected"] = pop_sum

    return ch_df


# below uses the exactextract library to mask a raster with a set of geometries.
# this function is used to calculate the number of people affected by a hazard and
# incorporates partial pixels, so that if a pixel is partially covered by the
# buffered hazard, the pixel value is adjusted by the proportion of the pixel
# that is covered by the hazard. this is done by calculating the overlap ratio
# of the buffered hazard with each pixel, and adjusting the pixel value by that
# ratio. the sum of all adjusted pixel values is the number of people affected
# by the hazard.

# github for this function here: https://github.com/isciences/exactextract/blob/master/python/src/exactextract/exact_extract.py

def mask_raster_with_geoms(ch_df: gpd.GeoDataFrame, raster_path: str):

    # Open the raster file
    with rasterio.open(raster_path) as src:
        # Ensure CRS alignment
        if ch_df.crs != src.crs:
            print(f"Reprojecting GeoDataFrame to match raster CRS: {src.crs}")
            ch_df = ch_df.to_crs(src.crs)

    # Use exact_extract to calculate population sums for each geometry
    ch_df["num_people_affected"] = exact_extract(raster_path, ch_df, "sum")

    return ch_df

# take path to climate hazard shapefile, path to additional
# geographies, such as ZCTAs or counties, and a raster dataset of gridded
# population data, and return a dataframe with the number of people affected by
# each climate hazard in each geography
# ---------------------------------------------------------
# read in a climate hazard shapefile or spatial unit shapefile (counties,
# zcta, etc) in parquet format that contains a string column with the geom ID
# and a geography column, but nothing else.
def find_num_people_affected_by_geo(
    path_to_hazards: str,
    path_to_additional_geos: str,
    raster_path: str,
    buffer_dist_large: int = 10000,
    buffer_dist_small: int = 5000,
    area_thresh_for_large_buffer: int = 10000,
):
    # prep data
    # get ID, hazard geom, best UTM, buffer dist, and buffered geom in WGS84
    # also get ad geos in WGS84
    ch_shp, ad_geo = prep_data(
        path_to_hazards=path_to_hazards,
        path_to_additional_geos=path_to_additional_geos,
        buffer_dist_large=buffer_dist_large,
        buffer_dist_small=buffer_dist_small,
        area_thresh_for_large_buffer=area_thresh_for_large_buffer,
    )

    # find overlapping buffered hazards
    # select and rename columns in filtered_ch - ID, and set buffered hazard to be geom
    ch_shp = ch_shp[["ID_climate_hazard", "buffered_hazard"]]
    # rename buffered hazard to geometry
    ch_shp = ch_shp.rename(columns={"buffered_hazard": "geometry"})
    ch_shp = ch_shp.set_geometry("geometry")
    # call
    ch_shp = combine_overlapping_geometries(ch_shp, id_column="ID_climate_hazard")

    # intersect buffered hazards w spatial units
    # intersection gives new dataframe with hazard ID, unit ID, and piece of geo
    # intersecting w each buffered hazard
    # set active geom to buffered hazard
    ch_shp = ch_shp.set_geometry("geometry", crs="EPSG:4326")
    unit_hazard_intersection = gpd.overlay(ch_shp, ad_geo, how="intersection")

    # get bounding boxes for unit hazard pieces combined
    unit_hazard_intersection = add_bounding_box_col(
        unit_hazard_intersection, target_col="geometry"
    )

    # find num of people affected by each piece
    num_af = mask_raster_with_geoms(unit_hazard_intersection, raster_path)

    # select columns
    num_af = num_af[["ID_climate_hazard", "ID_unit", "num_people_affected"]]

    return num_af


# find number of people by geography
# finds number of people residing in each additional geography
def find_number_of_people_residing_by_geo(
    path_to_additional_geos: str, raster_path: str
):

    # prep geographies
    ad_geo = prep_geographies(path_to_additional_geos, geo_type="spatial_unit")

    # add bounding box col
    ad_geo = add_bounding_box_col(ad_geo, target_col="geometry")

    # mask raster and find people by geo
    num_people_by_geo = mask_raster_with_geoms(ad_geo, raster_path)

    return num_people_by_geo
