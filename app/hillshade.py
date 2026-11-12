"""Windowed, versioned provincial hillshade mosaics from the private R2 bucket."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import threading

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds,Resampling
from rasterio.transform import from_bounds
from PIL import Image

from . import r2gis
from .memory_utils import release_memory

PREFIX='hillshade/provinces/'
CREDIT='Hillshade: uploaded provincial data | Areas outside coverage: light gray'
LOCK=threading.Lock()


def catalog():
    files=[]
    for page in r2gis.client().get_paginator('list_objects_v2').paginate(Bucket=r2gis.BUCKET,Prefix=PREFIX):
        for item in page.get('Contents',[]):
            if item['Key'].lower().endswith(('.tif','.tiff')) and item.get('Size',0)>0:
                files.append({key:str(item.get(key,'')) if key=='LastModified' else item.get(key) for key in ('Key','Size','ETag','LastModified')})
    return sorted(files,key=lambda item:item['Key'])


def cache_paths(source):
    from .downloader import ROOT
    version=json.dumps(source,sort_keys=True)
    key=hashlib.sha256(version.encode()).hexdigest()
    folder=ROOT/'hillshade';folder.mkdir(parents=True,exist_ok=True)
    return folder/(key+'.tif'),folder/(key+'.json')


def raster_info(dataset):
    if not dataset.crs or dataset.count!=1 or dataset.dtypes[0]!='uint8':
        raise ValueError('Uploaded hillshade must be a georeferenced, single-band 8-bit GeoTIFF.')
    bounds=list(transform_bounds(dataset.crs,'EPSG:3857',*dataset.bounds,densify_pts=21))
    if not all(math.isfinite(value) for value in bounds):raise ValueError('Invalid hillshade extent.')
    return {'bounds':bounds,'crs':dataset.crs.to_string(),'width':dataset.width,'height':dataset.height,'nodata':dataset.nodata}


def local_file(source):
    from .downloader import TRANSFER
    path,_=cache_paths(source)
    if not path.exists():
        with LOCK:
            if not path.exists():
                staging=path.with_suffix('.part')
                try:
                    r2gis.client().download_file(r2gis.BUCKET,source['Key'],str(staging),Config=TRANSFER)
                    if staging.stat().st_size!=source['Size']:raise ValueError('Incomplete hillshade download.')
                    staging.replace(path)
                finally:staging.unlink(missing_ok=True)
    return path


def metadata(source):
    path,index=cache_paths(source)
    if index.exists():return {**source,**json.loads(index.read_text())}
    info=None
    if path.exists():
        with rasterio.open(path) as dataset:info=raster_info(dataset)
    else:
        # Most GeoTIFF directories fit in 16 KB. Larger directories grow on demand.
        for limit in (16384,65536,262144,1048576):
            obj=r2gis.client().get_object(Bucket=r2gis.BUCKET,Key=source['Key'],Range=f'bytes=0-{min(limit,source["Size"])-1}')
            try:payload=obj['Body'].read()
            finally:obj['Body'].close()
            try:
                with MemoryFile(payload) as memory,memory.open() as dataset:info=raster_info(dataset)
                break
            except (rasterio.errors.RasterioError,ValueError):
                if len(payload)>=source['Size']:raise
        if info is None:
            with rasterio.open(local_file(source)) as dataset:info=raster_info(dataset)
    staging=index.with_suffix('.json.part')
    staging.write_text(json.dumps(info));staging.replace(index)
    return {**source,**info}


def intersects(first,second):
    return first[0]<second[2] and first[2]>second[0] and first[1]<second[3] and first[3]>second[1]


def background(bounds,size,sources=None):
    sources=catalog() if sources is None else sources
    if not sources:return None,'Uploaded provincial hillshade is unavailable'
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='hillshade-index') as pool:
        entries=list(pool.map(metadata,sources))
    release_memory()
    selected=[entry for entry in entries if intersects(bounds,entry['bounds'])]
    if not selected:return None,'Uploaded provincial hillshade does not cover this map view'
    canvas=np.full((size[1],size[0]),240,dtype='uint8')
    covered=np.zeros(canvas.shape,dtype=bool)
    transform=from_bounds(*bounds,size[0],size[1])
    # Read only the requested view from each intersecting province; no national raster.
    for source in selected:
        with rasterio.Env(GDAL_CACHEMAX=8*1024*1024):
            with rasterio.open(local_file(source)) as dataset:
                with WarpedVRT(dataset,crs='EPSG:3857',transform=transform,width=size[0],height=size[1],resampling=Resampling.bilinear,add_alpha=True,warp_mem_limit=16) as view:
                    pixels=view.read(1)
                    valid=(view.read(view.count)>0)&~covered
                    canvas[valid]=pixels[valid]
                    covered|=valid
        del pixels,valid
        release_memory(full=False)
    if not covered.any():return None,'Uploaded provincial hillshade has no valid pixels in this map view'
    return Image.fromarray(canvas).convert('RGB'),CREDIT
