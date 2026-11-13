"""Bounded polygon uploads, temporary opaque handles, and spatial source matching."""
import json
import re
import shutil
import tempfile
import time
import threading
import uuid
import zipfile
from pathlib import Path,PurePosixPath

import geopandas as gpd
import numpy as np
import pyogrio
import shapely

MAX_BYTES=20*1024*1024
MAX_EXPANDED=80*1024*1024
MAX_FEATURES=5000
MAX_COORDINATES=300000
TTL=24*3600
ROOT=Path(tempfile.gettempdir())/'map-custom-boundaries'


def clean_expired():
    ROOT.mkdir(parents=True,exist_ok=True)
    for path in ROOT.iterdir():
        if path.is_dir() and re.fullmatch(r'[a-f0-9]{32}',path.name):
            try:
                if time.time()-path.stat().st_mtime>TTL:shutil.rmtree(path,ignore_errors=True)
            except FileNotFoundError:pass


def parse(raw,filename,crs_override=None):
    if not raw:raise ValueError('Choose a boundary file.')
    if len(raw)>MAX_BYTES:raise ValueError('Boundary upload must be 20 MB or smaller.')
    ext=Path(filename).suffix.lower()
    drivers={'.geojson':'GeoJSON','.json':'GeoJSON','.gpkg':'GPKG','.zip':'ESRI Shapefile'}
    if ext not in drivers:raise ValueError('Use GeoJSON, GeoPackage (.gpkg), or a Shapefile ZIP.')
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/('boundary'+ext)
        path.write_bytes(raw)
        if ext=='.zip':
            try:
                with zipfile.ZipFile(path) as archive:
                    entries=archive.infolist()
                    if len(entries)>50 or sum(i.file_size for i in entries)>MAX_EXPANDED:raise ValueError('Shapefile ZIP exceeds the expanded size limit (80 MB).')
                    for i in entries:
                        p=PurePosixPath(i.filename.replace('\\','/'))
                        if p.is_absolute() or '..' in p.parts or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe path in Shapefile ZIP.')
                    shapes=[i for i in entries if not i.is_dir() and i.filename.lower().endswith('.shp')]
                    if len(shapes)!=1:raise ValueError('ZIP must contain exactly one Shapefile boundary.')
                    stem=shapes[0].filename.rsplit('.',1)[0].lower()
                    found=set()
                    for i in entries:
                        name,_,suffix=i.filename.rpartition('.')
                        suffix=suffix.lower()
                        if name.lower()==stem and suffix in ('shp','shx','dbf','prj','cpg'):
                            if suffix in found:raise ValueError('Duplicate Shapefile components in ZIP.')
                            found.add(suffix)
                            with archive.open(i) as src,(Path(folder)/('boundary.'+suffix)).open('wb') as dst:shutil.copyfileobj(src,dst,length=1024*1024)
                    if not {'shp','shx','dbf'}<=found:raise ValueError('Shapefile ZIP needs .shp, .shx, and .dbf files.')
                    path=Path(folder)/'boundary.shp'
            except (zipfile.BadZipFile,RuntimeError):raise ValueError('Could not read this Shapefile ZIP.')
        try:
            layers=pyogrio.list_layers(path)
            if len(layers)!=1:raise ValueError('Upload a file with exactly one polygon layer.')
            info=pyogrio.read_info(path)
            if info['driver']!=drivers[ext]:raise ValueError('File contents do not match the uploaded boundary format.')
            if info['features']>MAX_FEATURES:raise ValueError('Boundary contains too many features (maximum 5,000).')
            frame=pyogrio.read_dataframe(path,columns=[],max_features=MAX_FEATURES+1)
        except ValueError:raise
        except Exception:raise ValueError('Could not read the boundary. Check the file and its coordinate reference system.')
    if len(frame)>MAX_FEATURES:raise ValueError('Boundary contains too many features (maximum 5,000).')
    if frame.empty or frame.geometry.isna().any() or frame.geometry.is_empty.any():raise ValueError('Boundary must contain non-empty polygons.')
    if not frame.geom_type.isin(['Polygon','MultiPolygon']).all():raise ValueError('Boundary must contain polygons only, not points or lines.')
    if int(shapely.get_num_coordinates(frame.geometry.array).sum())>MAX_COORDINATES:raise ValueError('Boundary is too detailed. Simplify it to fewer than 300,000 vertices before uploading.')
    from pyproj import CRS
    coords=shapely.get_coordinates(frame.geometry.array)
    if not np.isfinite(coords).all():raise ValueError('Boundary has non-finite coordinates.')
    correction={'source_crs':None,'target_crs':'EPSG:4326','axis_swapped':False,'assumed_crs':False,'crs_override':False,'repaired':not bool(frame.geometry.is_valid.all())}
    if crs_override:
        if not re.fullmatch(r'(?:EPSG|ESRI):[0-9]{3,6}',str(crs_override).upper()):raise ValueError('Choose a valid original CRS.')
        try:frame=frame.set_crs(CRS.from_user_input(crs_override),allow_override=True)
        except Exception:raise ValueError('Original CRS was not recognized. Choose the CRS used to create the boundary.')
        correction['crs_override']=True
    if frame.crs is None:
        # Only geographic coordinates in the Philippine extent are unambiguous.
        normal=bool(((coords[:,0]>=116)&(coords[:,0]<=128)&(coords[:,1]>=3)&(coords[:,1]<=23)).all())
        swapped=bool(((coords[:,1]>=116)&(coords[:,1]<=128)&(coords[:,0]>=3)&(coords[:,0]<=23)).all())
        if not (normal or swapped):raise ValueError('Projected coordinates have no CRS. Select the original CRS below, or include the Shapefile .prj file; the datum and UTM zone cannot be guessed reliably.')
        frame=frame.set_crs(4326)
        correction['assumed_crs']=True
    correction['source_crs']=frame.crs.to_string()
    if frame.crs.is_geographic and bool(((coords[:,1]>=116)&(coords[:,1]<=128)&(coords[:,0]>=3)&(coords[:,0]<=23)).all()):
        frame.geometry=shapely.transform(frame.geometry.array,lambda xy:xy[:,[1,0]])
        correction['axis_swapped']=True
    try:frame=frame.to_crs(4326)
    except Exception:raise ValueError('Boundary could not be reprojected. Select its correct original CRS.')
    coords=shapely.get_coordinates(frame.geometry.array)
    if not np.isfinite(coords).all() or np.abs(coords[:,0]).max()>180 or np.abs(coords[:,1]).max()>85:raise ValueError('Boundary coordinates are invalid or outside the supported map extent. Check its CRS.')
    geometry=shapely.union_all(shapely.make_valid(frame.geometry.array,method='structure',keep_collapsed=False))
    if geometry.is_empty or geometry.geom_type not in ('Polygon','MultiPolygon'):raise ValueError('Boundary could not be repaired into a valid polygon.')
    result=gpd.GeoDataFrame(geometry=[geometry],crs=4326)
    if float(result.to_crs(6933).geometry.area.sum())<=0:raise ValueError('Boundary must have a positive area.')
    correction['reprojected']=not CRS.from_user_input(correction['source_crs']).equals(CRS.from_epsg(4326))
    result.attrs['normalization']=correction
    return result


UPLOAD_LOCK=threading.Lock()


def save(raw,filename,crs_override=None):
    if not UPLOAD_LOCK.acquire(blocking=False):raise ValueError('Another boundary is being validated. Please retry shortly.')
    try:return _save(raw,filename,crs_override)
    finally:UPLOAD_LOCK.release()


def _save(raw,filename,crs_override=None):
    frame=parse(raw,filename,crs_override)
    clean_expired()
    if sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())>500*1024*1024:raise ValueError('Boundary uploads are busy. Please try again later.')
    token=uuid.uuid4().hex
    folder=ROOT/token;folder.mkdir()
    label=re.sub(r'[^\w .()-]+',' ',Path(filename.replace('\\','/')).stem,flags=re.UNICODE).strip()[:70] or 'Custom boundary'
    meta={'boundary_id':token,'name':label,'area_ha':float(frame.to_crs(6933).geometry.area.sum()/10000),'bounds':frame.total_bounds.tolist(),'expires_in':TTL,'normalization':frame.attrs.get('normalization',{})}
    (folder/'boundary.geojson').write_text(frame.to_json(drop_id=True))
    (folder/'metadata.json').write_text(json.dumps(meta))
    return meta


def load(token):
    if not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{32}',token):raise ValueError('Invalid uploaded boundary. Upload the file again.')
    folder=ROOT/token
    try:
        if time.time()-folder.stat().st_mtime>TTL:raise ValueError('Uploaded boundary has expired. Upload the file again.')
        meta=json.loads((folder/'metadata.json').read_text())
        return gpd.read_file(folder/'boundary.geojson'),meta
    except (FileNotFoundError,OSError):raise ValueError('Uploaded boundary is unavailable after a restart. Upload the file again.')


def provinces(path,boundary):
    """Read one candidate geometry at a time, retaining only names and feature IDs."""
    info=pyogrio.read_info(path)
    mask=boundary.to_crs(info['crs']).geometry.iloc[0]
    names=['ADM2_EN','PROVINCE','PROV_NAME','NAME_1','NAME_2']
    columns=[name for name in names if name in info['fields']]
    result=[];offset=0
    while True:
        frame=pyogrio.read_dataframe(path,columns=columns,bbox=mask.bounds,skip_features=offset,max_features=1,fid_as_index=True)
        if frame.empty:break
        offset+=len(frame)
        row=frame.iloc[0];geometry=row.geometry
        if geometry is None or geometry.is_empty:continue
        geometry=shapely.make_valid(geometry,method='structure',keep_collapsed=False)
        if not geometry.intersects(mask) or geometry.intersection(mask).area<=0:continue
        name=next((str(row[n]) for n in columns if row.get(n) is not None and str(row[n]).lower() not in ('nan','none','')),'')
        if not name:raise ValueError('A matching province has no readable name in the source catalog.')
        result.append((int(frame.index[0]),name))
    if not result:raise ValueError('Uploaded boundary does not overlap the available Philippine provincial datasets.')
    return result


class LocalOnlyStorage:
    """Custom-boundary rasters remain local instead of entering shared R2 caches."""
    def __init__(self,storage):self.storage=storage
    def __getattr__(self,name):
        if name=='put_object':raise AttributeError(name)
        return getattr(self.storage,name)
