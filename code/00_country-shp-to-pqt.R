# Load necessary libraries
library(sf)         # For vector data (GeoJSON, Shapefiles)
library(terra)      # For raster data (GeoTIFFs)
library(dplyr)      # For data manipulation
library(sfarrow)
library(nanoparquet)
library(dplyr)

# Set input directory for current data
input_dir <- "/Users/ninaflores/Library/CloudStorage/OneDrive-SharedLibraries-UW/casey_cohort - Documents/data/geo_boundaries/raw_data/country_admin"
output_dir <- "/Users/ninaflores/Library/CloudStorage/OneDrive-SharedLibraries-UW/casey_cohort - Documents/data/geo_boundaries/processed_data/country_admin"

pqt_file <- file.path(output_dir, paste0("admin2_geom.parquet"))
pqt_file2 <- file.path(output_dir, paste0("admin2_names_to_ids.parquet"))
pqt_file3 <- file.path(output_dir, paste0("country_geom.parquet"))
pqt_file4 <- file.path(output_dir, paste0("country_geom_filtered.parquet"))


# 1. Read the OGIM GeoJSON file and filter to just ID and geometry
data <- st_read(file.path(input_dir, "lbd_standard_admin_2.shp")) 
  
admin_bound <- data %>% 
  st_make_valid() %>%
  select(geo_id, geometry) %>%
  rename("id" = "geo_id")

st_write_parquet(admin_bound, pqt_file)

name <- data %>%
  as.data.frame() %>%
  select(-geometry) %>%
  rename("id" = "geo_id")

write_parquet(name, pqt_file2)


# Ensure country boundaries are valid and merged by country name
country_bound <- data %>%
  st_make_valid() %>%
  group_by(ADM0_NAME) %>%
  summarize(geometry = st_union(geometry)) %>%
  st_make_valid()

st_write_parquet(country_bound, pqt_file3)


# Ensure country_bound has WGS 84 CRS
if (st_crs(country_bound)$epsg != 4326) {
  country_bound <- st_transform(country_bound, crs = 4326)
}

st_write_parquet(country_bound, pqt_file3)

oil_dir <- "~/Desktop/projects/casey cohort/OGW/data/drive-download"
ogim <- st_read(file.path(oil_dir, "OGIM_full-002.geojson")) %>%
  filter(CATEGORY == "OIL AND NATURAL GAS WELLS") %>%
  select(id, geometry)

# Ensure ogim has WGS 84 CRS
if (st_crs(ogim)$epsg != 4326) {
  ogim <- st_transform(ogim, crs = 4326)
}

# Buffer the country boundaries by 10km (only for filtering)
country_bound_buffered <- st_buffer(country_bound, dist = 10000)

country_bound_buffered <- country_bound_buffered %>%
  st_make_valid()

# Identify original country boundaries that are within 10km of any well
countries_with_wells <- country_bound %>%
  filter(st_intersects(country_bound_buffered$geometry, st_union(ogim), sparse = FALSE))

# Save the filtered original country boundaries
st_write_parquet(countries_with_wells, pqt_file4)


