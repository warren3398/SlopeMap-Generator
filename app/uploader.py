import os, json, secrets, tempfile, zipfile
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from shapely.geometry import mapping, MultiPolygon, Polygon
from shapely.validation import make_valid
from .main import engine, read_upload

router = APIRouter(prefix="/admin", tags=["GIS data administration"])

def authorize(token):
    expected = os.getenv("GIS_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "GIS_ADMIN_TOKEN not configured on Render")
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(401, "Invalid administrator token")

def polygon_geojson(geom):
    if geom is None or geom.is_empty:
        return None
    g = make_valid(geom)
    if isinstance(g, Polygon):
        g = MultiPolygon([g])
    elif not isinstance(g, MultiPolygon):
        parts = [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
        if not parts:
            return None
        g = MultiPolygon(parts)
    return json.dumps(mapping(g), separators=(",", ":"))

@router.get("/upload", response_class=HTMLResponse)
def uploader_page():
    return Path("static/uploader.html").read_text()

@router.post("/preview")
async def preview(file: UploadFile = File(...), x_admin_token: str | None = Header(None)):
    authorize(x_admin_token)
    gdf = read_upload(await file.read(), file.filename or "")
    return {"features": len(gdf), "columns": [str(c) for c in gdf.columns if c != gdf.geometry.name],
            "crs_output": "EPSG:4326", "geometry_types": gdf.geom_type.value_counts().to_dict(),
            "sample_properties": [{str(k): str(v)[:120] for k,v in row.items() if k != gdf.geometry.name} for row in gdf.head(2).to_dict("records")]}

@router.post("/import")
async def import_shapefile(
    file: UploadFile = File(...),
    kind: str = Form(...),
    dataset_name: str = Form(""),
    class_field: str = Form(""),
    code_field: str = Form(""),
    name_field: str = Form(""),
    level: str = Form(""),
    parent_field: str = Form(""),
    source_name: str = Form(""),
    x_admin_token: str | None = Header(None)
):
    authorize(x_admin_token)
    if kind not in ("slope","boundary"):
        raise HTTPException(400, "Invalid import type")
    if kind == "boundary" and level not in ("region","province","municipality","barangay"):
        raise HTTPException(400, "Invalid administrative level")
    gdf = read_upload(await file.read(), file.filename or "")
    if len(gdf) > 100000:
        raise HTTPException(413, "Maximum 100,000 features per upload; split large files")
    required = [class_field] if kind == "slope" else [code_field, name_field]
    if any(not field or field not in gdf.columns for field in required):
        raise HTTPException(400, "Select valid source attribute columns")
    if parent_field and parent_field not in gdf.columns:
        raise HTTPException(400, "Parent field does not exist")
    rows = []
    for _, record in gdf.iterrows():
        geometry = polygon_geojson(record.geometry)
        if not geometry:
            continue
        if kind == "slope":
            val = record[class_field]
            if val is None or str(val).strip() == "":
                continue
            rows.append({"slope_class":str(val).strip(),"geom":geometry,"source_feature_id":str(record.name)})
        else:
            code, name = record[code_field], record[name_field]
            if code is None or name is None or not str(code).strip() or not str(name).strip():
                continue
            parent = str(record[parent_field]).strip() if parent_field and record[parent_field] is not None else None
            rows.append({"code":str(code).strip(),"name":str(name).strip(),"parent":parent or None,"geom":geometry})
    if not rows:
        raise HTTPException(400, "No valid polygons to import")
    if kind == "boundary" and len({r["code"] for r in rows}) != len(rows):
        raise HTTPException(400, "Duplicate boundary codes in upload")
    try:
        with engine().begin() as conn:
            if kind == "slope":
                result = conn.execute(text("""INSERT INTO slopemap.slope_datasets(dataset_name,source_name,source_crs,classification_field)
                    VALUES(:name,:source,:crs,:field) RETURNING id"""),
                    {"name":dataset_name.strip() or (file.filename or "Master slope"),
                     "source":source_name or file.filename, "crs":"EPSG:4326","field":class_field})
                dataset_id = result.scalar_one()
                sql = text("""INSERT INTO slopemap.master_slope(dataset_id,source_feature_id,slope_class,geom)
                    VALUES(:dataset_id,:source_feature_id,:slope_class,
                    extensions.ST_Multi(extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(:geom),4326)))""")
                for i in range(0,len(rows),250):
                    conn.execute(sql,[dict(r,dataset_id=dataset_id) for r in rows[i:i+250]])
            else:
                sql = text("""INSERT INTO slopemap.admin_boundaries(boundary_code,boundary_name,level,parent_code,source_name,source_crs,geom)
                    VALUES(:code,:name,:level,:parent,:source,'EPSG:4326',
                    extensions.ST_Multi(extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(:geom),4326)))
                    ON CONFLICT (boundary_code) DO UPDATE SET boundary_name=EXCLUDED.boundary_name,
                    level=EXCLUDED.level,parent_code=EXCLUDED.parent_code,source_name=EXCLUDED.source_name,
                    geom=EXCLUDED.geom""")
                for i in range(0,len(rows),250):
                    conn.execute(sql,[dict(r,level=level,source=source_name or file.filename) for r in rows[i:i+250]])
    except Exception:
        raise HTTPException(500, "Database import failed. Transaction rolled back; check schema and database credentials.")
    return {"status":"imported","kind":kind,"imported_features":len(rows),
            "skipped_features":len(gdf)-len(rows),**({"dataset_id":dataset_id} if kind=="slope" else {})}
