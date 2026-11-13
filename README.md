# SlopeMap GIS Downloader

Select an island group and region, optionally a province, municipality or barangay, then download JPEG, Shapefile ZIP or GeoPackage. Leaving Province blank selects the whole region. Files prepare automatically in a single background worker and download when ready; there is no Generate step or map preview.

The JPEG follows the slope-map reference: framed map, north arrow, green information panels, slope legend with A–F ranges, class-area summary and map information. Images are 3840 × 2406, 300 DPI. Image-area figures use equal-area classified pixels and are explicitly labeled as estimates. Source coverage is shown on the map. Vector downloads retain full-detail clipped geometry, original attributes, `slope_code`, exact equal-area `clip_ha`, and `source_fid`.

Data comes from the existing private Cloudflare R2 bucket `slopemap-gis-data`. Runtime variables are `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`. Provincial files are discovered under `slope/<Province>_slope.gpkg`; only selected provinces are downloaded. Palawan uses both `Palawan_slope_north.gpkg` and `Palawan_slope_south.gpkg`, with overlapping areas counted once (north takes precedence). Missing provincial files are reported explicitly. The retired master file is not used by the download page. Derived artifacts are cached under `exports/provincial-v3/` and small display rasters under `exports/regional-rasters/`. Cache identity includes source versions and selected boundary geometry. Cache writes are optional: when unavailable, files are served from local ephemeral storage.

The boundary catalog uses attribute-only parent relationships. Regional boundaries are assembled from the selected region's actual provinces, because the existing `regions.gpkg` contains province records. Unknown or missing boundary names are not replaced with internal FIDs.

Coverage is constrained by the existing slope data. The existing minimum 50% classified-coverage policy is retained; lower coverage fails explicitly instead of publishing an effectively empty map. Downloads below 98% coverage show a source-coverage notice. This change does not fabricate missing nationwide data or run DEM generation.

Geometry processing is serialized, spatially filtered and batched. Invalid polygons use GEOS structure repair. JPEG display boundaries are generalized at pixel scale; larger source files are burned directly to a small raster in a bounded GDAL subprocess; Python does not load their polygon geometry. JPEGs are masked to the selected boundary once after rasterization, avoiding per-feature vector intersections. Raster buffers and R2 transfer queues are bounded, and native memory is released between batches. GIS output is written to disk rather than accumulated into a browser GeoJSON payload. Source changes invalidate the caches. HTTP requests poll preparation status rather than waiting through long GIS work. A server restart produces an explicit interrupted-download message.

The Docker service keeps its existing Uvicorn port binding, adds the raster/Pillow dependencies and DejaVu fonts, and starts Uvicorn with `exec`. Existing preprocessing workflows and legacy endpoints remain available; the new page does not depend on them.

Run checks with `pytest -q tests` and `node tests/test_download_ui.js` (test dependencies: pytest and httpx).

JPEGs use the uploaded provincial GeoTIFFs in `slopemap-gis-data/hillshade/provinces/` beneath the 70% classification overlay. GeoTIFF headers are indexed with bounded S3 range requests; only files intersecting the expanded map viewport are downloaded and mosaicked. Rasterio reads the requested window using the file's native CRS and NoData mask, then reprojects it to EPSG:3857. Neighboring provinces fill the map context; uncovered areas are light gray. Header and file caches use object versions, and a changed hillshade ETag invalidates the JPEG cache while retaining the classification raster cache. `/api/hillshade-sources` reports the uploaded catalog. No external hillshade tile service is used.

The JPEG layout omits the large top classification heading, enlarges the map frame, and aligns the sidebar at the top. Location names remain in the sidebar. The scale bar is recalibrated to the larger map viewport.

SlopeMap JPEGs include a light-blue ocean and shoreline strokes. A bundled OCHA/NAMRIA land layer (via geoBoundaries) supplies Philippine shorelines; Natural Earth supplies surrounding land context. Ocean is determined from land geometry, independently of hillshade NoData. Inland holes are filled, so missing hillshade and inland lakes are not shown as ocean. Only original coastal exteriors are drawn, avoiding administrative boundaries and artificial viewport-edge lines. Ocean and Shoreline symbols are included in the legend. Data provenance and attribution are in `app/data/SOURCES.md`.

4K JPEG exports are drawn natively at 3840 × 2406 pixels, maintaining the page aspect ratio. Fonts and strokes render at the target size, classification rasters use a 2560-pixel long edge, and basemaps are requested at the native map viewport size. JPEGs use quality 95, 4:4:4 chroma (no subsampling), and 300 DPI metadata. The source GIS geometry and original terrain/imagery resolution remain the basis of the map.


### Custom boundary uploads

Open **Advanced Options → Upload your own boundary** to clip JPEG, Shapefile ZIP, or GeoPackage downloads to a custom polygon. A valid upload replaces the administrative selection; remove it to return to the area selectors. The NPAAAD/SAFDZ layer selection still applies. Provincial source datasets are selected spatially, including boundaries crossing province borders and multipart Palawan slope sources.

Accepted: GeoJSON (`.geojson`/`.json`), single-layer GeoPackage (`.gpkg`), or one Shapefile ZIP containing `.shp`, `.shx`, `.dbf`, and `.prj` (optional `.cpg`). Upload limit: 20 MB, 5,000 features, 300,000 coordinates; expanded ZIP limit: 80 MB. Only polygons and multipolygons are accepted. CRS is required (standard GeoJSON defaults to WGS 84); projected data is reprojected automatically. Invalid polygons are repaired when possible; holes and multipart boundaries are retained. Unsupported coverage produces an explicit error.

Uploads and their derived exports remain in temporary local storage under unpredictable boundary IDs, expire after 24 hours, and are lost on service restart. Custom rasters and exports are excluded from shared R2 caches. Cleanup runs on new uploads. No uploaded attributes are retained.
