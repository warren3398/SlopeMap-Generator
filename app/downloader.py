"""One-click, cached slope downloads at every administrative level."""
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import pyogrio
import shapely
from boto3.s3.transfer import TransferConfig
from fastapi import APIRouter,Body,HTTPException
from fastapi.responses import FileResponse

from . import r2gis
from .memory_utils import release_memory

router=APIRouter()
ROOT=Path('/tmp/slope-direct-downloads')
CACHE_LOCK=threading.Lock()
TRANSFER=TransferConfig(max_concurrency=2,max_io_queue=4,io_chunksize=256*1024)
CATALOG=json.loads(Path(__file__).with_name('administrative_catalog.json').read_text())
CODES=['130000000','140000000','010000000','020000000','030000000','040000000','170000000','050000000','060000000','180000000','070000000','080000000','090000000','100000000','110000000','120000000','160000000','190000000']
REGIONS={code:region for code,region in zip(CODES,CATALOG['regions'])}


def slug(value):return re.sub(r'[^a-z0-9]+','-',str(value).lower()).strip('-')


def canonical(value):
    name=slug(value).removeprefix('province-of-')
    name={'city-of-cotabato':'cotabato-city','city-of-isabela':'isabela-city'}.get(name,name)
    return CATALOG['aliases'].get(name,name)


def object_file(kind):
    key=r2gis.KEYS[kind]
    meta=r2gis.client().head_object(Bucket=r2gis.BUCKET,Key=key)
    stamp=json.dumps([key,meta.get('ETag'),str(meta.get('LastModified',''))])
    digest=hashlib.sha256(stamp.encode()).hexdigest()
    path=ROOT/'sources'/(kind+'-'+digest+'.gpkg')
    if not path.exists():
        with CACHE_LOCK:
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True)
                staging=path.with_suffix('.part')
                try:
                    r2gis.client().download_file(r2gis.BUCKET,key,str(staging),Config=TRANSFER)
                    staging.replace(path)
                finally:staging.unlink(missing_ok=True)
    return path,{'Key':key,'ETag':meta.get('ETag'),'LastModified':str(meta.get('LastModified',''))}


NAMES={
 'province':('ADM2_EN','ADM2ALT1EN','ADM2ALTEN','PROVINCE','PROV_NAME','NAME_1','NAME_2'),
 'municipality':('ADM3_EN','MUNICIPALITY','CITY_MUN','MUN_NAME','NAME_2','NAME_3'),
 'barangay':('ADM4_EN','BARANGAY','BRGY_NAME','NAME_3','NAME_4'),
}


def actual_name(row,kind):
    for field in NAMES[kind]:
        value=row.get(field)
        if value is not None and str(value).strip().lower() not in ('','none','nan','nat','null','<na>'):
            return str(value).strip()
    raise ValueError('Administrative name is missing from the boundary catalog.')


@lru_cache(maxsize=6)
def attributes(path):
    info=pyogrio.read_info(path)
    wanted=set(sum((list(names) for names in NAMES.values()),[]))|{'ADM1_PCODE','ADM2_PCODE','ADM3_PCODE','GID_1','GID_2','GID_3','NAME_1','NAME_2','NAME_3'}
    columns=[str(c) for c in info['fields'] if c in wanted]
    return pyogrio.read_dataframe(path,columns=columns,read_geometry=False,fid_as_index=True)


def province_rows(region_code):
    region=REGIONS.get(str(region_code).zfill(9))
    if region is None:raise ValueError('Unknown region.')
    path,_=object_file('province')
    table=attributes(str(path))
    expected={canonical(name) for name in region['provinces']}
    all_named={canonical(name) for item in REGIONS.values() for name in item['provinces']}
    fallback='PH'+str(region_code).zfill(9)[:2]
    rows=[]
    for fid,row in table.iterrows():
        try:name=actual_name(row,'province')
        except ValueError:
            logging.warning('Province boundary has no readable name: %s',fid)
            continue
        normalized=canonical(name)
        if normalized in expected or (normalized not in all_named and str(row.get('ADM1_PCODE','')).strip().upper()==fallback):
            rows.append((int(fid),name))
    return sorted(rows,key=lambda item:item[1])


def selection_boundary(level,fid,display=False):
    if level=='region':
        code=str(fid).zfill(9)
        members=province_rows(code)
        if not members:raise ValueError('No provinces are available for this region in the boundary catalog.')
        path,_=object_file('province')
        geometries=[]
        crs=None
        for province_id,_ in members:
            from .boundary_geometry import read_geometry
            frame=read_geometry(path,province_id)
            if frame.empty:continue
            crs=frame.crs
            if display:
                bounds=frame.total_bounds
                tolerance=max(bounds[2]-bounds[0],bounds[3]-bounds[1])/1600/4
                frame.geometry=frame.geometry.simplify(tolerance,preserve_topology=False)
                release_memory()
            invalid=~frame.geometry.is_valid
            if invalid.any():frame.loc[invalid,frame.geometry.name]=shapely.make_valid(frame.loc[invalid].geometry.array,method='structure',keep_collapsed=False)
            geometries.extend(frame.geometry)
        if not geometries or crs is None:raise ValueError('Regional boundary is unavailable.')
        geometry=shapely.union_all(geometries)
        return gpd.GeoDataFrame(geometry=[geometry],crs=crs),REGIONS[code]['name']
    path,_=object_file(level)
    print('SLOPE_BOUNDARY reading',level,fid,flush=True)
    from .boundary_geometry import read_geometry
    frame=read_geometry(path,int(fid))
    print('SLOPE_BOUNDARY read',level,fid,len(frame),flush=True)
    if frame.empty:raise ValueError('Selected administrative boundary was not found.')
    title=actual_name(attributes(str(path)).loc[int(fid)],level)
    frame=frame[[frame.geometry.name]].copy()
    invalid=~frame.geometry.is_valid
    if invalid.any():
        print('SLOPE_BOUNDARY repairing',level,fid,flush=True)
        frame.loc[invalid,frame.geometry.name]=shapely.make_valid(frame.loc[invalid].geometry.array,method='structure',keep_collapsed=False)
    return frame,title


def split_parent(code,expected):
    level,_,fid=str(code or '').partition(':')
    if level!=expected or not fid.isdigit():raise ValueError('Select a valid parent administrative area.')
    return fid


def spatial_children(kind,path,parent_code):
    parent_kind,_,fid=parent_code.partition(':')
    boundary,_=selection_boundary(parent_kind,fid)
    info=pyogrio.read_info(path)
    mask=boundary.to_crs(info['crs']).geometry.iloc[0]
    result=[]
    offset=0
    while True:
        batch=pyogrio.read_dataframe(path,bbox=mask.bounds,skip_features=offset,max_features=128,fid_as_index=True)
        if batch.empty:break
        offset+=len(batch)
        for fid,row in batch.iterrows():
            if row.geometry is not None and not row.geometry.is_empty and mask.covers(row.geometry.representative_point()):
                result.append({'code':kind+':'+str(fid),'name':actual_name(row,kind)})
    return result


@router.get('/api/slope-boundaries')
def boundary_catalog(level:str,parent_code:str|None=None):
    try:
        if level=='region':
            return [{'code':'region:'+code,'name':region['name'],'island':'Luzon' if index<8 else 'Visayas' if index<12 else 'Mindanao'} for index,(code,region) in enumerate(REGIONS.items())]
        if level=='province':
            fid=split_parent(parent_code,'region')
            return [{'code':'province:'+str(key),'name':name} for key,name in province_rows(fid)]
        if level not in ('municipality','barangay'):raise ValueError('Invalid administrative level.')
        parent_kind='province' if level=='municipality' else 'municipality'
        fid=split_parent(parent_code,parent_kind)
        parent_path,_=object_file(parent_kind)
        parent=attributes(str(parent_path))
        if int(fid) not in parent.index:raise ValueError('Parent boundary was not found.')
        row=parent.loc[int(fid)]
        path,_=object_file(level)
        table=attributes(str(path))
        if level=='barangay' and 'GID_2' in table and row.get('GID_2') is not None:
            selected=table[table.GID_2.astype(str)==str(row['GID_2'])]
        elif level=='municipality' and 'NAME_1' in table:
            name=canonical(actual_name(row,'province'))
            selected=table[table.NAME_1.map(canonical)==name]
        else:selected=table.iloc[:0]
        if selected.empty:
            result=spatial_children(level,path,parent_code)
        else:result=[{'code':level+':'+str(key),'name':actual_name(value,level)} for key,value in selected.iterrows()]
        return sorted(result,key=lambda item:item['name'])
    except ValueError as exc:raise HTTPException(400,str(exc))
    except HTTPException:raise
    except Exception:
        logging.exception('Slope boundary catalog failed')
        raise HTTPException(503,'Boundary list is temporarily unavailable. Please retry.')


class LocalSource:
    def __init__(self,sources):self.sources={source['Key']:source for source in sources}
    def __getattr__(self,key):return getattr(r2gis.client(),key)
    def download_file(self,bucket,key,target,**kwargs):
        from .slope_sources import local_source
        path=local_source(self.sources[key])
        try:os.link(path,target)
        except OSError:shutil.copyfile(path,target)


def restore(key,path):
    try:
        obj=r2gis.client().get_object(Bucket=r2gis.BUCKET,Key=key)
        staging=path.with_suffix(path.suffix+'.part')
        try:
            with staging.open('wb') as stream:
                for chunk in obj['Body'].iter_chunks(chunk_size=1024*1024):
                    if chunk:stream.write(chunk)
            if staging.stat().st_size==0:raise ValueError('Empty derived cache object')
            expected=obj.get('ContentLength')
            if expected is not None and staging.stat().st_size!=expected:raise ValueError('Incomplete derived cache object')
            staging.replace(path)
        finally:obj['Body'].close();staging.unlink(missing_ok=True)
        return True
    except Exception:return False


def persist(path,key):
    try:r2gis.client().upload_file(str(path),r2gis.BUCKET,key,Config=TRANSFER)
    except Exception:logging.warning('Prepared slope download is cached locally; shared cache write unavailable')


def prepare_file(params):
    from .jpeg_layout import LAYOUT_VERSION
    from .raster_exports import render_region
    from .vector_exports import build_geopackage,shapefile_from_geopackage
    level,fid,fmt=params['level'],params['fid'],params['format']
    print('SLOPE_PREPARE boundary',level,fid,fmt,flush=True)
    boundary,title=selection_boundary(level,fid,display=(fmt=='jpeg'))
    release_memory()
    print('SLOPE_PREPARE boundary_ready',level,fid,flush=True)
    boundary_ha=float(boundary.to_crs(6933).geometry.area.sum()/10000)
    if boundary_ha<=0:raise ValueError('Selected boundary is empty.')
    from .slope_sources import select_sources
    sources=select_sources(level,fid)
    print('SLOPE_PREPARE sources_ready',len(sources),flush=True)
    fingerprint=hashlib.sha256(b''.join(boundary.to_crs(4326).geometry.to_wkb())).hexdigest()
    identity=json.dumps(['provincial-v3',level,fid,sources,fingerprint],sort_keys=True)
    digest=hashlib.sha256(identity.encode()).hexdigest()
    folder=ROOT/'exports'/digest;folder.mkdir(parents=True,exist_ok=True)
    ext={'jpeg':'jpg','zip':'zip','gpkg':'gpkg'}[fmt]
    filename=slug(title)+'_slope.'+ext
    target=folder/(f'slope-{LAYOUT_VERSION}.jpg' if fmt=='jpeg' else 'slope.'+ext)
    prefix='exports/provincial-v3/'+digest+'/'
    if target.exists() or restore(prefix+target.name,target):
        if not (folder/'coverage.json').exists():restore(prefix+'coverage.json',folder/'coverage.json')
        return target,filename
    release_memory()
    print('SLOPE_PREPARE export_start',fmt,flush=True)
    storage=LocalSource(sources)
    if fmt=='jpeg':
        # Discard the full coastline before source rendering; JPEG needs only pixel-scale detail.
        bounds=boundary.total_bounds
        tolerance=max(bounds[2]-bounds[0],bounds[3]-bounds[1])/1600/4
        boundary.geometry=boundary.geometry.simplify(tolerance,preserve_topology=False)
        release_memory()
        staging=folder/'layout.part.jpg'
        try:metadata=render_region(storage,r2gis.BUCKET,sources,'slope',title,staging,ROOT/'rasters',boundary=boundary,boundary_ha=boundary_ha)
        except Exception:staging.unlink(missing_ok=True);raise
        staging.replace(target)
        metadata.update(coverage_pct=metadata['total_ha']/boundary_ha*100,boundary_ha=boundary_ha,name=title)
        (folder/'coverage.json').write_text(json.dumps(metadata,indent=2))
        persist(folder/'coverage.json',prefix+'coverage.json')
    else:
        gpkg=folder/'slope.gpkg'
        if not gpkg.exists() and not restore(prefix+'slope.gpkg',gpkg):
            staging=folder/'slope.part.gpkg'
            try:
                metadata=build_geopackage(storage,r2gis.BUCKET,sources,'slope',boundary,staging)
                coverage=metadata['total_ha']/boundary_ha*100
                if coverage<50:raise ValueError(f'Existing slope source covers only {coverage:.1f}% of the selected area. Complete slope source data is needed for this export.')
                metadata.update(boundary_ha=boundary_ha,coverage_pct=coverage,name=title)
                (folder/'coverage.json').write_text(json.dumps(metadata,indent=2))
                staging.replace(gpkg)
                persist(folder/'coverage.json',prefix+'coverage.json')
                persist(gpkg,prefix+'slope.gpkg')
            except Exception:staging.unlink(missing_ok=True);raise
        if not (folder/'coverage.json').exists():restore(prefix+'coverage.json',folder/'coverage.json')
        if fmt=='zip':
            staging=folder/'slope.part.zip'
            shapefile_from_geopackage(gpkg,staging,folder)
            staging.replace(target)
    if fmt!='gpkg':persist(target,prefix+target.name)
    return target,filename


JOBS={}
JOB_LOCK=threading.Lock()
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='slope-download')


def run_job(job_id,params):
    with JOB_LOCK:JOBS[job_id]['status']='processing'
    try:
        release_memory()
        path,name=prepare_file(params)
        result={'status':'ready','path':str(path),'name':name}
        try:result['coverage_pct']=json.loads((path.parent/'coverage.json').read_text()).get('coverage_pct')
        except (OSError,ValueError):pass
    except ValueError as exc:result={'status':'failed','error':str(exc)}
    except Exception:
        logging.exception('Slope download preparation failed')
        result={'status':'failed','error':'Could not prepare this download. Please retry.'}
    release_memory()
    with JOB_LOCK:JOBS[job_id].update(result,updated=time.monotonic())


@router.get('/api/slope-coverage')
def slope_coverage(level:str,fid:str):
    if level not in ('region','province','municipality','barangay') or not fid.isdigit() or len(fid)>10:
        raise HTTPException(400,'Invalid location selection.')
    try:
        from .slope_sources import select_sources
        sources=select_sources(level,fid)
        return {'status':'possible','message':'Uploaded provincial slope data is available. Actual polygon coverage is checked during preparation.','source_count':len(sources)}
    except ValueError as exc:
        return {'status':'outside','message':str(exc)}
    except Exception:
        logging.exception('Slope coverage preflight failed')
        raise HTTPException(503,'Slope coverage check is temporarily unavailable.')

@router.post('/api/prepare-download')
def start_download(data:dict=Body(...)):
    level=str(data.get('level',''));fid=str(data.get('fid',''));fmt=str(data.get('format',''))
    if level not in ('region','province','municipality','barangay') or not fid.isdigit() or len(fid)>10 or fmt not in ('jpeg','zip','gpkg'):
        raise HTTPException(400,'Invalid download selection.')
    if level=='region':fid=fid.zfill(9)
    params={'level':level,'fid':fid,'format':fmt}
    now=time.monotonic()
    with JOB_LOCK:
        expired=[key for key,value in JOBS.items() if value['status'] in ('ready','failed') and now-value['updated']>3600]
        for key in expired:del JOBS[key]
        active=next((key for key,value in JOBS.items() if value['params']==params and value['status'] in ('queued','processing')),None)
        if active:return {'job_id':active,'status':JOBS[active]['status']}
        if sum(job['status'] in ('queued','processing') for job in JOBS.values())>=4:raise HTTPException(429,'Downloads are busy. Please retry shortly.')
        job_id=uuid.uuid4().hex
        JOBS[job_id]={'status':'queued','params':params,'updated':now}
    EXECUTOR.submit(run_job,job_id,params)
    return {'job_id':job_id,'status':'processing'}


@router.get('/api/download-status/{job_id}')
def download_status(job_id:str):
    with JOB_LOCK:job=JOBS.get(job_id,{}).copy()
    if not job:raise HTTPException(404,'Download preparation was interrupted by a server restart. Please retry.')
    return {'status':job['status'],'error':job.get('error'),'coverage_pct':job.get('coverage_pct'),'download_url':'/api/download-file/'+job_id if job['status']=='ready' else None}


@router.get('/api/download-file/{job_id}')
def download_file(job_id:str):
    with JOB_LOCK:job=JOBS.get(job_id,{}).copy()
    if not job or job['status']!='ready':raise HTTPException(409,'This download is not ready. Please prepare it again.')
    path=Path(job['path'])
    if not path.exists():raise HTTPException(404,'Prepared download has expired. Please retry.')
    media={'jpeg':'image/jpeg','zip':'application/zip','gpkg':'application/geopackage+sqlite3'}[job['params']['format']]
    return FileResponse(path,filename=job['name'],media_type=media)


@router.get('/api/slope-sources')
def uploaded_sources():
    from .slope_sources import catalog
    groups=catalog()
    return {'mode':'provincial','province_groups':len(groups),'file_count':sum(map(len,groups.values())),'sources':groups}
