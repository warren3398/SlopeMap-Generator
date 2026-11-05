import os, json, tempfile, zipfile, threading
from pathlib import Path
from functools import lru_cache
import boto3
import geopandas as gpd
import pyogrio
from shapely.ops import unary_union
from shapely.geometry import mapping
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Body
from botocore.config import Config
from fastapi.responses import StreamingResponse
from io import BytesIO

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
    if ext not in (".zip",".gpkg",".geojson",".json",".kml",".kmz"):raise HTTPException(400,"Use Shapefile ZIP, GPKG, GeoJSON, KML or KMZ")
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/("parcel"+ext)
        path.write_bytes(raw)
        if ext==".kmz":
            try:
                with zipfile.ZipFile(path) as z:
                    entries=[i for i in z.infolist() if i.filename.lower().endswith(".kml") and i.file_size<50*1024*1024]
                    if not entries:raise HTTPException(400,"KMZ does not contain KML")
                    path=Path(d)/"parcel.kml"
                    path.write_bytes(z.read(entries[0]))
            except zipfile.BadZipFile:raise HTTPException(400,"Invalid KMZ")
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
        try:gdf=gpd.read_file(path,driver="KML" if path.suffix.lower()==".kml" else None)
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
    try:
        source=local_file(kind)
        source_crs=pyogrio.read_info(source)["crs"]
        bounds=gpd.GeoSeries([geom],crs=4326).to_crs(source_crs).total_bounds
        gdf=pyogrio.read_dataframe(source,bbox=tuple(bounds),use_arrow=False)
    except Exception as exc:raise HTTPException(422,"Cannot read "+kind+" GeoPackage: "+type(exc).__name__)
    if gdf.crs is None:raise HTTPException(422,kind+" dataset missing CRS")
    # Filter candidates in source CRS before transforming potentially large geometries.
    target_geom=gpd.GeoSeries([geom],crs=4326).to_crs(gdf.crs).iloc[0]
    gdf=gdf[gdf.geometry.intersects(target_geom)].copy()
    return gdf.to_crs(4326)

@router.get("/gis/status")
def status():
    return {"storage":"Cloudflare R2","bucket":BUCKET,"configured":configured(),"datasets":list(KEYS)}

@router.get("/gis/inspect-slope")
def inspect_slope():
    path=local_file("slope")
    info=pyogrio.read_info(path)
    fields=[str(x) for x in info["fields"]]
    sample=pyogrio.read_dataframe(path,read_geometry=False,max_features=12)
    return {"count":int(info["features"]),"columns":fields,"samples":[{str(k):str(v)[:90] for k,v in row.items()} for row in sample.to_dict("records")]}

@router.get("/gis/inspect-boundaries")
def inspect_boundaries():
    result={}
    for level in ("region","province","municipality","barangay"):
        try:
            path=local_file(level)
            info=pyogrio.read_info(path)
            df=pyogrio.read_dataframe(path,max_features=3)
            cols=[str(c) for c in df.columns if c!=df.geometry.name]
            result[level]={"count":int(info["features"]),"columns":cols,"samples":[{c:str(row[c])[:70] for c in cols} for _,row in df.iterrows()]}
        except Exception as exc:
            result[level]={"error":type(exc).__name__}
    return result

def region_frame(code):
    provinces=pyogrio.read_dataframe(local_file("province"),columns=["ADM1_PCODE","ADM1_EN"])
    selected=provinces[provinces["ADM1_PCODE"].astype(str)==code]
    if selected.empty:raise HTTPException(404,"Region not found")
    return unary_union(selected.to_crs(4326).geometry.values)

def selected_geometry(code):
    level,_,identifier=code.partition(":")
    if level=="region":return region_frame(identifier)
    if level not in ("province","municipality","barangay") or not identifier.isdigit():
        raise HTTPException(400,"Invalid administrative boundary")
    df=pyogrio.read_dataframe(local_file(level),fids=[int(identifier)])
    if df.empty:raise HTTPException(404,"Administrative boundary not found")
    return valid_poly(df.to_crs(4326).geometry.iloc[0])

@router.get("/boundaries")
def boundaries(level:str,parent_code:str|None=None):
    if level not in ("region","province","municipality","barangay"):
        raise HTTPException(400,"Invalid level")
    if level=="region":
        df=pyogrio.read_dataframe(local_file("province"),columns=["ADM1_EN","ADM1_PCODE"])
        if not {"ADM1_EN","ADM1_PCODE"}.issubset(df.columns):
            raise HTTPException(422,"Province dataset lacks ADM1_EN / ADM1_PCODE")
        unique=df[["ADM1_EN","ADM1_PCODE"]].dropna().drop_duplicates()
        return sorted([{"code":"region:"+str(r.ADM1_PCODE),"name":str(r.ADM1_EN)} for r in unique.itertuples(index=False)],key=lambda x:x["name"])
    source=local_file(level)
    info=pyogrio.read_info(source)
    if parent_code:
        geom=selected_geometry(parent_code)
        bbox=tuple(gpd.GeoSeries([geom],crs=4326).to_crs(info["crs"]).total_bounds)
        df=pyogrio.read_dataframe(source,bbox=bbox,fid_as_index=True)
        if not df.empty:
            df=df.to_crs(4326)
            # Require representative point inside the parent, avoiding neighbors along shared borders.
            df=df[df.geometry.representative_point().within(geom)]
    else:
        # Nationwide province GeoPackage has very large geometries. Dropdowns need names and FIDs only.
        # Avoid loading ~200 MB of polygon geometry into the Render free instance.
        name_field={"province":"ADM2_EN","municipality":"NAME_2","barangay":"NAME_3"}[level]
        df=pyogrio.read_dataframe(source,columns=[name_field],read_geometry=False,fid_as_index=True)
    col={"province":"ADM2_EN","municipality":"NAME_2","barangay":"NAME_3"}[level]
    if col not in df.columns:
        raise HTTPException(422,"Missing "+col+" in "+level+" dataset")
    return sorted([{"code":level+":"+str(fid),"name":str(r[col])} for fid,r in df.iterrows() if r[col] is not None],key=lambda x:x["name"])

@router.post("/generate")
async def generate(boundary_file:UploadFile|None=File(None),boundary_code:str|None=Form(None)):
    if bool(boundary_file)==bool(boundary_code):raise HTTPException(400,"Upload parcel or select administrative boundary")
    if boundary_file:
        parcels=read_parcel(await boundary_file.read(),boundary_file.filename or "")
        boundary=unary_union([valid_poly(g) for g in parcels.geometry])
    else:
        boundary=selected_geometry(boundary_code)
    if boundary.is_empty:raise HTTPException(400,"Empty boundary")
    slope=intersecting("slope",boundary)
    if slope.empty:return {"type":"FeatureCollection","features":[],"statistics":[],"total_classified_ha":0}
    field=slope_column([c for c in slope.columns if c!=slope.geometry.name])
    feats=[]; stats={}
    from pyproj import Transformer
    from shapely.ops import transform
    area_transform=Transformer.from_crs(4326,6933,always_xy=True).transform
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
        area=float(transform(area_transform,cut).area)/10000
        cls=str(r[field]);stats[cls]=stats.get(cls,0)+area
        feats.append({"type":"Feature","properties":{"slope_class":cls,"area_ha":round(area,3)},"geometry":mapping(cut)})
    return {"type":"FeatureCollection","features":feats,"statistics":[{"slope_class":k,"area_ha":round(v,3)} for k,v in sorted(stats.items())],"total_classified_ha":round(sum(stats.values()),3)}

@router.post("/download/shapefile")
def download_shapefile(payload:dict=Body(...)):
    features=payload.get("features",[])
    if not isinstance(features,list) or not features:
        raise HTTPException(400,"Generate a slope map before downloading the shapefile")
    if len(features)>200000:
        raise HTTPException(413,"Too many features for one shapefile")
    try:
        import tempfile
        import zipfile
        from pathlib import Path
        from shapely.geometry import shape
        records=[]
        for feat in features:
            props=feat.get("properties",{})
            geom=shape(feat["geometry"])
            if geom.is_empty or geom.geom_type not in ("Polygon","MultiPolygon"):continue
            records.append({"slope_cls":str(props.get("slope_class",""))[:20],"area_ha":float(props.get("area_ha",0)), "geometry":geom})
        if not records:raise HTTPException(400,"No polygon features to export")
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"slope_map.shp"
            gpd.GeoDataFrame(records,geometry="geometry",crs="EPSG:4326").to_file(path,driver="ESRI Shapefile",engine="pyogrio",encoding="UTF-8")
            buffer=BytesIO()
            with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
                for file in Path(folder).glob("slope_map.*"):archive.write(file,file.name)
            buffer.seek(0)
            return StreamingResponse(buffer,media_type="application/zip",headers={"Content-Disposition":'attachment; filename="slope_map_shapefile.zip"'})
    except HTTPException:raise
    except Exception as exc:raise HTTPException(422,"Shapefile export failed: "+str(exc)[:150])
