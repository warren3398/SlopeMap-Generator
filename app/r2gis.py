import os, json, tempfile, zipfile, threading, sqlite3
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

@router.get("/gis/preprocessing-status")
def preprocessing_status():
    # Lightweight R2-only status: do not download the nationwide province GeoPackage
    # merely to count features, which can exhaust the small Render web instance.
    prefix="prepared/provinces/v1/"
    count=0;empty_or_invalid=0;oversized=0;continuation=None
    while True:
        params={"Bucket":BUCKET,"Prefix":prefix,"MaxKeys":1000}
        if continuation:params["ContinuationToken"]=continuation
        response=client().list_objects_v2(**params)
        for o in response.get("Contents",[]):
            if not o["Key"].endswith(".geojson"):continue
            if o["Size"]<=150:empty_or_invalid+=1
            elif o["Size"]>50*1024*1024:oversized+=1
            else:count+=1
        if not response.get("IsTruncated"):break
        continuation=response.get("NextContinuationToken")
    return {"ready_provinces":count,"empty_or_invalid":empty_or_invalid,"oversized":oversized,"total_provinces":None,"storage":"Cloudflare R2","note":"Counts pre-generated province objects in R2. Total province count is omitted to avoid loading the nationwide boundary GeoPackage into Render memory."}

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
    # Region boundaries are a separate, much smaller GeoPackage.
    if code.isdigit():
        df=pyogrio.read_dataframe(local_file("region"),fids=[int(code)])
        if not df.empty:return valid_poly(df.to_crs(4326).geometry.iloc[0])
    # Backward-compatible ADM1_PCODE lookup for older region selectors.
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
        # The region dropdown and province filter MUST use the same ADM1_PCODE
        # from the province dataset. Do not assume PSGC code conventions.
        source=local_file("province")
        info=pyogrio.read_info(source)
        fields=list(info["fields"])
        region_code=next((c for c in ("ADM1_PCODE","REGION_CODE","REG_CODE","ADM1_CODE") if c in fields),None)
        region_name=next((c for c in ("ADM1_EN","REGION","REGION_NAME","REG_NAME") if c in fields),None)
        if not region_code or not region_name:raise HTTPException(422,"Province dataset is missing region name/code fields")
        df=pyogrio.read_dataframe(source,columns=[region_code,region_name],read_geometry=False)
        pairs={(str(code).strip(),str(name).strip()) for code,name in zip(df[region_code],df[region_name]) if code is not None and name is not None}
        return sorted([{"code":"region:"+code,"name":name} for code,name in pairs],key=lambda x:x["name"])
    source=local_file(level)
    info=pyogrio.read_info(source)
    if level=="province" and parent_code and parent_code.startswith("region:"):
        # Dropdown query uses attribute-only GeoPackage reads: no large province geometries.
        region_code=parent_code.partition(":")[2]
        fields=list(info["fields"])
        name_col=name_column(fields,"province")
        region_col=next((c for c in ("ADM1_PCODE","REGION_CODE","REG_CODE","ADM1_CODE") if c in fields),None)
        if region_col is None:raise HTTPException(422,"Province dataset has no region-code field")
        df=pyogrio.read_dataframe(source,columns=[name_col,region_col],read_geometry=False,fid_as_index=True)
        def same_region(v):
            return str(v or "").strip()==region_code
        df=df[df[region_col].map(same_region)]
        return sorted([{"code":"province:"+str(fid),"name":str(row[name_col])} for fid,row in df.iterrows() if row[name_col] is not None],key=lambda x:x["name"])
    if parent_code:
        geom=selected_geometry(parent_code)
        bbox=tuple(gpd.GeoSeries([geom],crs=4326).to_crs(info["crs"]).total_bounds)
        df=pyogrio.read_dataframe(source,bbox=bbox,fid_as_index=True)
        if not df.empty:
            df=df.to_crs(4326)
            df=df[df.geometry.representative_point().within(geom)]
    else:
        name_field=name_column(list(info["fields"]),level)
        df=pyogrio.read_dataframe(source,columns=[name_field],read_geometry=False,fid_as_index=True)
    col=name_column(list(df.columns),level)
    return sorted([{"code":level+":"+str(fid),"name":str(r[col])} for fid,r in df.iterrows() if r[col] is not None],key=lambda x:x["name"])

@router.get("/gis/province-tiles/{province_id}/metadata")
def province_tile_metadata(province_id:int):
    if province_id<1:raise HTTPException(400,"Invalid province FID")
    key=f"tiles/provinces/v1/{province_id}/metadata.json"
    try:
        obj=client().get_object(Bucket=BUCKET,Key=key)
        return json.loads(obj["Body"].read())
    except Exception as exc:
        if getattr(exc,"response",{}).get("Error",{}).get("Code") in ("NoSuchKey","404","NotFound"):
            raise HTTPException(404,"Raster tiles not yet prepared for this province")
        raise HTTPException(503,"Cannot load tile metadata")

@router.get("/gis/province-tiles/{province_id}/{z}/{x}/{y}.png")
def province_tile(province_id:int,z:int,x:int,y:int):
    if province_id<1 or not 0<=z<=14 or not 0<=x<2**z or not 0<=y<2**z:
        raise HTTPException(400,"Invalid tile coordinate")
    key=f"tiles/provinces/v1/{province_id}/{z}/{x}/{y}.png"
    try:
        obj=client().get_object(Bucket=BUCKET,Key=key)
    except Exception as exc:
        if getattr(exc,"response",{}).get("Error",{}).get("Code") in ("NoSuchKey","404","NotFound"):
            raise HTTPException(404,"Tile not found")
        raise HTTPException(503,"Tile storage unavailable")
    def chunks():
        try:
            for chunk in obj["Body"].iter_chunks(chunk_size=65536):
                if chunk:yield chunk
        finally:obj["Body"].close()
    return StreamingResponse(chunks(),media_type="image/png",
                             headers={"Cache-Control":"public,max-age=86400"})

# Pre-generated download catalog, following the NPAAAD-SAFDZ Map Downloader pattern.
# Keys: exports/slope/{province|municipality|barangay}/{boundary_fid}/slope.{jpg|zip}
@router.get("/api/slope-inventory")
def slope_inventory():
    storage=client()
    catalog={}
    token=None
    while True:
        args={"Bucket":BUCKET,"Prefix":"exports/slope/","MaxKeys":1000}
        if token:args["ContinuationToken"]=token
        page=storage.list_objects_v2(**args)
        for obj in page.get("Contents",[]):
            import re
            m=re.fullmatch(r"exports/slope/(province|municipality|barangay)/(\d+)/slope\.(jpg|zip)",obj["Key"])
            if m and obj.get("Size",0)>0:
                identity=(m.group(1),m.group(2))
                item=catalog.setdefault(identity,{"level":identity[0],"fid":identity[1],"name":identity[0].title()+" FID "+identity[1],"formats":[]})
                fmt="jpeg" if m.group(3)=="jpg" else "zip"
                if fmt not in item["formats"]:item["formats"].append(fmt)
        if not page.get("IsTruncated"):break
        token=page.get("NextContinuationToken")
    return {"items":list(catalog.values()),"pdf":False,"source":"Cloudflare R2 pre-generated exports"}

@router.get("/api/slope-download")
def slope_download(level:str,fid:int,format:str):
    if level not in ("province","municipality","barangay") or fid<0 or format not in ("jpeg","zip"):
        raise HTTPException(400,"Invalid slope export selection")
    ext="jpg" if format=="jpeg" else "zip"
    key=f"exports/slope/{level}/{fid}/slope.{ext}"
    try:obj=client().get_object(Bucket=BUCKET,Key=key)
    except Exception as exc:
        if getattr(exc,"response",{}).get("Error",{}).get("Code") in ("NoSuchKey","404","NotFound"):
            raise HTTPException(404,"This slope export is not yet pre-generated.")
        raise HTTPException(503,"Slope export storage is temporarily unavailable.")
    filename=f"slope_{level}_{fid}.{ext}"
    mime="image/jpeg" if ext=="jpg" else "application/zip"
    def chunks():
        try:
            for chunk in obj["Body"].iter_chunks(chunk_size=1024*1024):
                if chunk:yield chunk
        finally:obj["Body"].close()
    return StreamingResponse(chunks(),media_type=mime,headers={"Content-Disposition":f'attachment; filename="{filename}"',"Content-Length":str(obj["ContentLength"]),"Cache-Control":"no-store"})

@router.post("/generate")
async def generate(boundary_file:UploadFile|None=File(None),boundary_code:str|None=Form(None)):
    if bool(boundary_file)==bool(boundary_code):raise HTTPException(400,"Upload parcel or select administrative boundary")
    if boundary_code and boundary_code.startswith("province:"):
        province_id=boundary_code.split(":",1)[1]
        if not province_id.isdigit():raise HTTPException(400,"Invalid province identifier")
        key="prepared/provinces/v1/"+province_id+".geojson"
        # Stream R2 GeoJSON rather than decoding and duplicating tens of MB in
        # the small Render process. The browser receives the same JSON schema.
        try:
            obj=client().get_object(Bucket=BUCKET,Key=key)
        except Exception as exc:
            if getattr(exc,"response",{}).get("Error",{}).get("Code") in ("NoSuchKey","404","NotFound"):
                raise HTTPException(503,"This province has not yet been preprocessed.")
            raise HTTPException(503,"Cannot load prepared province: "+type(exc).__name__)
        size=int(obj.get("ContentLength",0))
        if size<=150:
            obj["Body"].close()
            raise HTTPException(422,"No slope polygons found for this province in the current source dataset.")
        if size>50*1024*1024:
            obj["Body"].close()
            raise HTTPException(413,"Prepared province geometry exceeds the browser size limit; vector tiles are required.")
        def stream_prepared():
            try:
                for chunk in obj["Body"].iter_chunks(chunk_size=256*1024):
                    if chunk:yield chunk
            finally:
                obj["Body"].close()
        return StreamingResponse(stream_prepared(),media_type="application/geo+json",
                                 headers={"Cache-Control":"private, max-age=300"})
    if boundary_file:
        parcels=read_parcel(await boundary_file.read(),boundary_file.filename or "")
        boundary=unary_union([valid_poly(g) for g in parcels.geometry])
    else:
        boundary=selected_geometry(boundary_code)
    if boundary.is_empty:raise HTTPException(400,"Empty boundary")
    # Process spatially filtered slope features in small batches to avoid Render OOM.
    # A nationwide GeoPackage can contain extremely detailed polygon geometry.
    from pyproj import Transformer, CRS
    from shapely.ops import transform
    from shapely.geometry import MultiPolygon
    source=local_file("slope")
    info=pyogrio.read_info(source)
    source_crs=info.get("crs")
    if not source_crs:raise HTTPException(422,"Slope dataset missing CRS")
    field=slope_column(info["fields"])
    to_source=Transformer.from_crs(4326,source_crs,always_xy=True).transform
    to_wgs=Transformer.from_crs(source_crs,4326,always_xy=True).transform
    to_area=Transformer.from_crs(4326,6933,always_xy=True).transform
    boundary_source=transform(to_source,boundary)
    bounds=boundary_source.bounds
    feats=[];stats={};batch_size=60
    province_view=bool(boundary_code and boundary_code.startswith("province:"))
    vertex_budget=0
    # Stream indexed rows through one SQLite cursor, rather than reopening GDAL
    # for every FID batch (which caused multi-minute requests and 502s).
    from shapely import from_wkb
    import time
    started=time.monotonic()
    def ident(name):
        return '"'+str(name).replace('"','""')+'"'
    db=sqlite3.connect(str(source))
    try:
        row=db.execute("SELECT table_name,column_name FROM gpkg_geometry_columns LIMIT 1").fetchone()
        if not row:raise HTTPException(422,"Slope GeoPackage has no geometry layer")
        table_name,geom_name=row
        rtree_name="rtree_"+table_name+"_"+geom_name
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(rtree_name,)).fetchone()
        if not exists:
            raise HTTPException(422,"Slope GeoPackage has no RTree spatial index.")
        table_info=db.execute("PRAGMA table_info("+ident(table_name)+")").fetchall()
        pk=next((r[1] for r in table_info if r[5]),"fid")
        sql=("SELECT t."+ident(geom_name)+",t."+ident(field)+" FROM "+ident(table_name)+
             " t JOIN "+ident(rtree_name)+" r ON t."+ident(pk)+"=r.id"+
             " WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?")
        cursor=db.execute(sql,(bounds[2],bounds[0],bounds[3],bounds[1]))
        while True:
            if time.monotonic()-started>85:
                raise HTTPException(413,"Province processing exceeded 85 seconds. Please select a municipality or barangay; a province-wide optimized dataset is required.")
            rows=cursor.fetchmany(batch_size)
            if not rows:break
            for blob,cls_value in rows:
                if blob is None:continue
                # GeoPackage binary header: 8 bytes plus optional XY/XYZ envelope.
                flags=blob[3]
                envelope=(flags>>1)&7
                envelope_bytes={0:0,1:32,2:48,3:48,4:64}.get(envelope)
                if envelope_bytes is None:continue
                g=from_wkb(bytes(blob)[8+envelope_bytes:])
                r={field:cls_value}

                g=valid_poly(g)
                if g is None or g.is_empty or not g.intersects(boundary_source):continue
                cut=valid_poly(g.intersection(boundary_source))
                if cut is None or cut.is_empty:continue
                if cut.geom_type=="GeometryCollection":
                    pieces=[p for p in cut.geoms if p.geom_type=="Polygon"]
                    pieces += [p for m in cut.geoms if p.geom_type=="MultiPolygon" for p in m.geoms]
                    if not pieces:continue
                    cut=MultiPolygon(pieces)
                if cut.geom_type not in ("Polygon","MultiPolygon"):continue
                cut=transform(to_wgs,cut)
                area=float(transform(to_area,cut).area)/10000
                cls=str(r[field]);stats[cls]=stats.get(cls,0)+area
                # Keep statistics from full-resolution geometry; generalize province display
                # geometry to keep GeoJSON usable on the free server and in Leaflet.
                display=cut.simplify(0.002,preserve_topology=True) if province_view else cut
                if province_view and len(str(mapping(display)))>250000:
                    display=cut.simplify(0.006,preserve_topology=True)
                geometry=mapping(display)
                vertex_budget+=len(str(geometry))
                if vertex_budget>45_000_000:
                    raise HTTPException(413,"The selected area exceeds the online map geometry limit even after optimization. Please select a smaller area.")
                feats.append({"type":"Feature","properties":{"slope_class":cls,"area_ha":round(area,3)},"geometry":geometry})
    finally:
        db.close()
    return {"type":"FeatureCollection","features":feats,"statistics":[{"slope_class":k,"area_ha":round(v,3)} for k,v in sorted(stats.items())],"total_classified_ha":round(sum(stats.values()),3),"geometry_generalized":province_view}

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
