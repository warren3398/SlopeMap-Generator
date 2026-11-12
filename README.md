# SlopeMap GIS Downloader

Select an island group and region, optionally a province, municipality or barangay, then download JPEG, Shapefile ZIP or GeoPackage. Leaving Province blank selects the whole region. Files prepare automatically in a single background worker and download when ready; there is no Generate step or map preview.

The JPEG follows the slope-map reference: framed map, north arrow, green information panels, slope legend with A–F ranges, class-area summary and map information. Images are 3000 × 1880, 180 DPI. Image-area figures use equal-area classified pixels and are explicitly labeled as estimates. Source coverage is shown on the map. Vector downloads retain full-detail clipped geometry, original attributes, `slope_code`, exact equal-area `clip_ha`, and `source_fid`.

Data comes from the existing private Cloudflare R2 bucket `slopemap-gis-data`. Runtime variables are `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`. Provincial files are discovered under `slope/<Province>_slope.gpkg`; only selected provinces are downloaded. Palawan uses both `Palawan_slope_north.gpkg` and `Palawan_slope_south.gpkg`, with overlapping areas counted once (north takes precedence). Missing provincial files are reported explicitly. The retired master file is not used by the download page. Derived artifacts are cached under `exports/provincial-v3/` and small display rasters under `exports/regional-rasters/`. Cache identity includes source versions and selected boundary geometry. Cache writes are optional: when unavailable, files are served from local ephemeral storage.

The boundary catalog uses attribute-only parent relationships. Regional boundaries are assembled from the selected region's actual provinces, because the existing `regions.gpkg` contains province records. Unknown or missing boundary names are not replaced with internal FIDs.

Coverage is constrained by the existing slope data. The existing minimum 50% classified-coverage policy is retained; lower coverage fails explicitly instead of publishing an effectively empty map. Downloads below 98% coverage show a source-coverage notice. This change does not fabricate missing nationwide data or run DEM generation.

Geometry processing is serialized, spatially filtered and batched. Invalid polygons use GEOS structure repair. JPEG display boundaries are generalized at pixel scale; larger source files are burned directly to a small raster in a bounded GDAL subprocess; Python does not load their polygon geometry. JPEGs are masked to the selected boundary once after rasterization, avoiding per-feature vector intersections. Raster buffers and R2 transfer queues are bounded, and native memory is released between batches. GIS output is written to disk rather than accumulated into a browser GeoJSON payload. Source changes invalidate the caches. HTTP requests poll preparation status rather than waiting through long GIS work. A server restart produces an explicit interrupted-download message.

The Docker service keeps its existing Uvicorn port binding, adds the raster/Pillow dependencies and DejaVu fonts, and starts Uvicorn with `exec`. Existing preprocessing workflows and legacy endpoints remain available; the new page does not depend on them.

Run checks with `pytest -q tests` and `node tests/test_download_ui.js` (test dependencies: pytest and httpx).

JPEGs use Esri World Imagery below a 70% classification overlay, with visible imagery credits. Tile requests have a time and tile-count limit and a local cache. If imagery cannot be fetched, the JPEG clearly reports that it is unavailable and retains a readable classification map. GIS geometry and class-area calculations use the source classification data.
