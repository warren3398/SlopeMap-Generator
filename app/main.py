import json, os, tempfile, zipfile
from pathlib import Path
import geopandas as gpd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, text
from shapely.ops import unary_union

app = FastAPI(title="SlopeMap Generator", version="0.1.0")
DB = os.getenv("DATABASE_URL", "")

def engine():
    if not DB:
        raise HTTPException(503, "DATABASE_URL is not configured. Import master slope data first.")
    url = DB
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url)

def read_upload(data, filename):
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Upload exceeds 50 MB")
    ext = Path(filename).suffix.lower()
    if ext not in (".zip", ".geojson", ".json", ".kml", ".gpkg"):
        raise HTTPException(400, "Upload ZIP shapefile, GeoJSON, KML, or GPKG")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / ("boundary" + ext)
        path.write_bytes(data)
        if ext == ".zip":
            try:
                with zipfile.ZipFile(path) as z:
                    members = z.infolist()
                    if len(members) > 100 or sum(x.file_size for x in members) > 150 * 1024 * 1024:
                        raise HTTPException(413, "ZIP is too large")
                    for item in members:
                        p = Path(item.filename)
                        if p.is_absolute() or ".." in p.parts or item.is_dir() and item.filename.startswith("/"):
                            raise HTTPException(400, "Unsafe ZIP path")
                    z.extractall(td)
            except zipfile.BadZipFile:
                raise HTTPException(400, "Invalid ZIP file")
            shp = list(Path(td).rglob("*.shp"))
            if len(shp) != 1:
                raise HTTPException(400, "ZIP must contain exactly one .shp")
            path = shp[0]
        try:
            gdf = gpd.read_file(path)
        except Exception:
            raise HTTPException(400, "Cannot read boundary file")
    if gdf.empty or gdf.crs is None:
        raise HTTPException(400, "Boundary empty or missing CRS")
    if not gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise HTTPException(400, "Polygon boundary required")
    return gdf.to_crs(4326)

@app.get("/", response_class=HTMLResponse)
def home():
    return Path("static/index.html").read_text()

@app.get("/health")
def health():
    return {"status": "ok", "database_configured": bool(DB)}

@app.get("/boundaries")
def boundaries(level: str, parent_code: str | None = None):
    if level not in ("region", "province", "municipality", "barangay"):
        raise HTTPException(400, "Invalid level")
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT code,name FROM admin_boundaries WHERE level=:level AND (:parent IS NULL OR parent_code=:parent) ORDER BY name"), {"level": level, "parent": parent_code}).mappings().all()
    return list(map(dict, rows))

@app.post("/generate")
async def generate(boundary_file: UploadFile | None = File(None), boundary_code: str | None = Form(None)):
    if bool(boundary_file) == bool(boundary_code):
        raise HTTPException(400, "Provide uploaded parcel or administrative boundary")
    if boundary_file:
        geom = unary_union(read_upload(await boundary_file.read(), boundary_file.filename or "").geometry.make_valid().values)
        geojson = json.dumps(geom.__geo_interface__)
    else:
        with engine().connect() as conn:
            geojson = conn.execute(text("SELECT ST_AsGeoJSON(geom) FROM admin_boundaries WHERE code=:code"), {"code": boundary_code}).scalar()
        if not geojson:
            raise HTTPException(404, "Administrative boundary not found")
    sql = text("""WITH b AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(:geom),4326) AS geom),
    clipped AS (SELECT s.slope_class, ST_CollectionExtract(ST_Intersection(ST_MakeValid(s.geom), ST_MakeValid(b.geom)),3) AS geom
    FROM master_slope s CROSS JOIN b WHERE ST_Intersects(s.geom,b.geom))
    SELECT slope_class, ST_AsGeoJSON(geom) AS geometry,
    ST_Area(ST_Transform(geom,6933))/10000.0 AS area_ha
    FROM clipped WHERE NOT ST_IsEmpty(geom)""")
    with engine().connect() as conn:
        rows = conn.execute(sql, {"geom": geojson}).mappings().all()
    features, stats = [], {}
    for row in rows:
        area = float(row["area_ha"])
        cls = str(row["slope_class"])
        stats[cls] = stats.get(cls, 0) + area
        features.append({"type": "Feature", "properties": {"slope_class": cls, "area_ha": round(area,3)}, "geometry": json.loads(row["geometry"])})
    return {"type": "FeatureCollection", "features": features,
            "statistics": [{"slope_class": k, "area_ha": round(v,3)} for k,v in sorted(stats.items())],
            "total_classified_ha": round(sum(stats.values()),3)}
