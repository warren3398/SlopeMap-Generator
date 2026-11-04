import os, json, tempfile, zipfile, threading
from pathlib import Path
from functools import lru_cache
import boto3
import geopandas as gpd
import pyogrio
from shapely.ops import unary_union
from shapely.geometry import mapping
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from botocore.config import Config

router = APIRouter()
BUCKET = "slopemap-gis-data"
KEYS = {"slope":"slope/philippines_master_slope.gpkg","region":"boundaries/regions.gpkg","province":"boundaries/provinces.gpkg","municipality":"boundaries/municipalities.gpkg","barangay":"boundaries/barangays.gpkg"}
CACHE = Path("/tmp/slopemap-r2-cache")
LOCK = threading.Lock()

def configured():
    return all(os.getenv(k) for k in ("R2_ACCOUNT_ID","R2_ACCESS_KEY_ID","R2_SECRET_ACCESS_KEY"))

@lru_cache(maxsize=1)
def client():
    if not configured():
        raise HTTPException(503,"R2 credentials not configured on Render. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.")
    return boto3.client("s3",endpoint_url="https://"+os.environ["R2_ACCOUNT_ID"]+".r2.cloudflarestorage.com",region_name="auto",aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],config=Config(signature_version="s3v4",retries={"max_attempts":3}))

def local_file(kind):
    if kind not in KEYS: raise HTTPException(400,"Invalid dataset")
    CACHE.mkdir(parents=True,exist_ok=True)
    path=CACHE/(kind+".gpkg")
    if path.exists() and path.stat().st_size>0: return path
    with LOCK:
        if path.exists() and path.stat().st_size>0: return path
        tmp=path.with_suffix(".download")
        try:
            client().download_file(BUCKET,KEYS[kind],str(tmp))
            tmp.replace(path)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(503,"Cannot download "+kind+" from private R2: "+type(exc).__name__)
    return path

def read_parcel(raw,filename):
    if len(raw)>50*1024*1024: raise HTTPException(413,"Parcel upload exceeds 50 MB")
    ext=Path(filename).suffix.lower()
    if ext not in (".zip",".gpkg",".geojson",".json"):raise HTTPException(400,"Use Shapefile ZIP, GPKG or GeoJSON")
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/("parcel"+ext)
        path.write_bytes(raw)
        if ext==".zip":
            try:
                with zipfile.ZipFile(path) as z:
                    infos=z.infolist()
                    if len(infos)>100 or sum(i.file_size for i in infos)>250*1024*1024:raise HTTPException(413,"ZIP too large")
                    for i in infos:
                        p=Path(i.filename)
                        if p.is_absolute() or ".." in p.parts or i.is_dir() and i.filename.startswith("/"):raise HTTPException(400,"Unsafe ZIP")
                    z.extractall(d)
            except zipfile.BadZipFile:raise HTTPException(400,"Invalid ZIP")
            files=list(Path(d).rglob("*.shp"))
            if len(files)!=1:raise HTTPException(400,"ZIP must have exactly one shapefile")
            path=files[0]
        try:gdf=gpd.read_file(path)
        except Exception:raise HTTPException(400,"Cannot read parcel")
    if gdf.empty or gdf.crs is None:raise HTTPException(400,"Parcel empty or CRS missing")
    if not gdf.geom_type.isin(["Polygon","MultiPolygon"]).all():raise HTTPException(400,"Parcel must contain polygons")
    return gdf.to_crs(4326)

def name_column(columns,level):
    names={ "region":["REGION","REG_NAME","REGION_NAME","ADM1_EN","NAME_1"],
            "province":["PROVINCE","PROV_NAME","PROV","ADM2_EN","NAME_2"],
            "municipality":["CITY_MUN","MUNICIPALITY","MUN_NAME","CITY","ADM3_EN","NAME_3"],
            "barangay":["BARANGAY","BRGY_NAME","BRGY","ADM4_EN","NAME_4"]}
    lookup={str(c).upper():c for c in columns}
    for c in names[level]:
        if c in lookup:return lookup[c]
    for c in columns:
        if "NAME" in str(c).upper():return c
    raise HTTPException(422,"Cannot identify "+level+" name column. Available: "+", ".join(map(str,columns)))

def slope_column(columns):
    lookup={str(c).upper():c for c in columns}
    for name in ("SLOPE_CLASS","SLOPE_CLS","SLOPE","CLASS","SLOPE_RANGE","SLOPE_CAT","GRIDCODE"):
        if name in lookup:return lookup[name]
    raise HTTPException(422,"Cannot identify slope classification column. Available: "+", ".join(map(str,columns)))

def valid_poly(geometry):
    if geometry is None or geometry.is_empty:return geometry
    return geometry if geometry.is_valid else geometry.buffer(0)

def intersecting(kind,geom):
    # GeoPackage uses spatial index when the layer provides one.
    bounds=geom.bounds
    try: gdf=gpd.read_file(local_file(kind),bbox=bounds)
    except Exception as exc:raise HTTPException(422,"Cannot read "+kind+" GeoPackage: "+type(exc).__name__)
    if gdf.crs is None:raise HTTPException(422,kind+" dataset missing CRS")
    gdf=gdf.to_crs(4326)
    return gdf[gdf.geometry.intersects(geom)].copy()

@router.get("/gis/status")
def status():
    return {"storage":"Cloudflare R2","bucket":BUCKET,"configured":configured(),"datasets":list(KEYS)}

@router.get("/boundaries")
def boundaries(level:str,parent_code:str|None=None):
    if level not in ("region","province","municipality","barangay"):raise HTTPException(400,"Invalid level")
    # Parent selection uses level:row-id; no assumption about source PSGC fields.
    if parent_code:
        parent_level,_,row=parent_code.partition(":")
        if parent_level not in KEYS or parent_level=="slope" or not row.isdigit():raise HTTPException(400,"Invalid parent")
        try:parent=gpd.read_file(local_file(parent_level),rows=slice(int(row),int(row)+1))
        except Exception:raise HTTPException(422,"Cannot read selected parent")
        if parent.empty:raise HTTPException(404,"Parent not found")
        parent=parent.to_crs(4326)
        # Read row IDs from a separate FID column where available; do not invent positional IDs.
        # For listing, use GeoPackage feature IDs via pyogrio fid_as_index.
        df=pyogrio.read_dataframe(local_file(level),bbox=tuple(parent.to_crs(pyogrio.read_info(local_file(level))["crs"]).total_bounds),fid_as_index=True)
        df=df.to_crs(4326)
        df=df[df.geometry.intersects(parent.geometry.iloc[0])]
    else:
        df=pyogrio.read_dataframe(local_file(level),fid_as_index=True)
    col=name_column([c for c in df.columns if c!=df.geometry.name],level)
    out=[]
    for fid,r in df.iterrows():
        value=r[col]
        if value is None:continue
        out.append({"code":level+":"+str(fid),"name":str(value)})
    return sorted(out,key=lambda r:r["name"])

@router.post("/generate")
async def generate(boundary_file:UploadFile|None=File(None),boundary_code:str|None=Form(None)):
    if bool(boundary_file)==bool(boundary_code):raise HTTPException(400,"Upload parcel or select administrative boundary")
    if boundary_file:
        parcels=read_parcel(await boundary_file.read(),boundary_file.filename or "")
        boundary=unary_union([valid_poly(g) for g in parcels.geometry])
    else:
        level,_,fid=boundary_code.partition(":")
        if level not in ("region","province","municipality","barangay") or not fid.isdigit():raise HTTPException(400,"Invalid boundary")
        df=pyogrio.read_dataframe(local_file(level),fids=[int(fid)])
        if df.empty:raise HTTPException(404,"Boundary not found")
        boundary=valid_poly(df.to_crs(4326).geometry.iloc[0])
    if boundary.is_empty:raise HTTPException(400,"Empty boundary")
    slope=intersecting("slope",boundary)
    if slope.empty:return {"type":"FeatureCollection","features":[],"statistics":[],"total_classified_ha":0}
    field=slope_column([c for c in slope.columns if c!=slope.geometry.name])
    feats=[]; stats={}
    for _,r in slope.iterrows():
        g=valid_poly(r.geometry)
        if g is None or g.is_empty:continue
        cut=valid_poly(g.intersection(boundary))
        if cut is None or cut.is_empty:continue
        # Retain polygonal pieces including tiny slivers.
        if cut.geom_type=="GeometryCollection":
            from shapely.geometry import MultiPolygon
            pieces=[p for p in cut.geoms if p.geom_type=="Polygon"]
            pieces += [p for m in cut.geoms if m.geom_type=="MultiPolygon" for p in m.geoms]
            if not pieces:continue
            cut=MultiPolygon(pieces)
        if cut.geom_type not in ("Polygon","MultiPolygon"):continue
        area=float(gpd.GeoSeries([cut],crs=4326).to_crs(6933).area.iloc[0])/10000
        cls=str(r[field]);stats[cls]=stats.get(cls,0)+area
        feats.append({"type":"Feature","properties":{"slope_class":cls,"area_ha":round(area,3)},"geometry":mapping(cut)})
    return {"type":"FeatureCollection","features":feats,"statistics":[{"slope_class":k,"area_ha":round(v,3)} for k,v in sorted(stats.items())],"total_classified_ha":round(sum(stats.values()),3)}
