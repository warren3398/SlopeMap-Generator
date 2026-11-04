CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS master_slope (id BIGSERIAL PRIMARY KEY, slope_class TEXT NOT NULL, geom geometry(MultiPolygon,4326) NOT NULL);
CREATE INDEX IF NOT EXISTS master_slope_geom_idx ON master_slope USING GIST(geom);
CREATE TABLE IF NOT EXISTS admin_boundaries (code TEXT PRIMARY KEY, name TEXT NOT NULL, level TEXT NOT NULL CHECK(level IN ('region','province','municipality','barangay')), parent_code TEXT REFERENCES admin_boundaries(code), geom geometry(MultiPolygon,4326) NOT NULL);
CREATE INDEX IF NOT EXISTS admin_boundaries_geom_idx ON admin_boundaries USING GIST(geom);
CREATE INDEX IF NOT EXISTS admin_boundaries_parent_idx ON admin_boundaries(parent_code);
