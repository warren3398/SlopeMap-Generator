"""Bounded, cached Esri World Imagery tiles aligned to Web Mercator map bounds."""
from concurrent.futures import ThreadPoolExecutor,as_completed,TimeoutError
from io import BytesIO
from math import floor,ceil,log2
from pathlib import Path
import logging
import time
from urllib.request import Request,urlopen

from PIL import Image

HALF_WORLD=20037508.342789244
CACHE=Path('/tmp/gis-satellite-tiles')
CREDIT='Satellite © Esri | Sources: Esri, Vantor, Earthstar Geographics, GIS User Community'
MAX_TILES=36


def tile_plan(bounds,size):
    left,bottom,right,top=bounds
    resolution=max((right-left)/size[0],(top-bottom)/size[1])
    zoom=max(0,min(19,round(log2(2*HALF_WORLD/(256*resolution)))))
    while True:
        count=2**zoom
        span=2*HALF_WORLD/count
        x0=max(0,min(count-1,floor((left+HALF_WORLD)/span)))
        x1=max(0,min(count-1,ceil((right+HALF_WORLD)/span)-1))
        y0=max(0,min(count-1,floor((HALF_WORLD-top)/span)))
        y1=max(0,min(count-1,ceil((HALF_WORLD-bottom)/span)-1))
        if (x1-x0+1)*(y1-y0+1)<=MAX_TILES or zoom==0:break
        zoom-=1
    extent=(-HALF_WORLD+x0*span,HALF_WORLD-(y1+1)*span,-HALF_WORLD+(x1+1)*span,HALF_WORLD-y0*span)
    return zoom,(x0,y0,x1,y1),extent


def decode(payload):
    with Image.open(BytesIO(payload)) as image:
        if image.size!=(256,256):raise ValueError('Unexpected satellite tile dimensions')
        return image.convert('RGB')


def fetch_tile(zoom,x,y):
    CACHE.mkdir(parents=True,exist_ok=True)
    path=CACHE/f'{zoom}-{x}-{y}.jpg'
    try:
        if time.time()-path.stat().st_mtime<7*86400:return decode(path.read_bytes())
    except (OSError,ValueError):pass
    url=f'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{y}/{x}'
    with urlopen(Request(url,headers={'User-Agent':'GISMapDownloader/1.0'}),timeout=5) as response:
        payload=response.read(2*1024*1024+1)
    if len(payload)>2*1024*1024:raise ValueError('Satellite tile is too large')
    image=decode(payload)
    staging=path.with_suffix('.part')
    try:staging.write_bytes(payload);staging.replace(path)
    finally:staging.unlink(missing_ok=True)
    return image


def prune_cache():
    try:
        entries=sorted((path.stat().st_mtime,path.stat().st_size,path) for path in CACHE.glob('*.jpg'))
        total=sum(size for _,size,_ in entries)
        for _,size,path in entries:
            if total<=64*1024*1024:break
            path.unlink(missing_ok=True);total-=size
    except OSError:pass


def background(bounds,size):
    """Return a registered RGB image and visible credit/status; never block GIS exports."""
    zoom,(x0,y0,x1,y1),extent=tile_plan(bounds,size)
    mosaic=Image.new('RGB',((x1-x0+1)*256,(y1-y0+1)*256),'#e8ece8')
    pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='satellite')
    futures={pool.submit(fetch_tile,zoom,x,y):(x,y) for y in range(y0,y1+1) for x in range(x0,x1+1)}
    received=0
    try:
        for future in as_completed(futures,timeout=15):
            try:
                tile=future.result()
                x,y=futures[future]
                mosaic.paste(tile,((x-x0)*256,(y-y0)*256))
                tile.close();received+=1
            except Exception:logging.getLogger(__name__).warning('Satellite tile unavailable')
    except TimeoutError:logging.getLogger(__name__).warning('Satellite background time budget reached')
    finally:pool.shutdown(wait=False,cancel_futures=True)
    prune_cache()
    if not received:
        mosaic.close()
        return None,'Satellite imagery unavailable'
    left,bottom,right,top=bounds
    xscale=mosaic.width/(extent[2]-extent[0])
    yscale=mosaic.height/(extent[3]-extent[1])
    crop=((left-extent[0])*xscale,(extent[3]-top)*yscale,(right-extent[0])*xscale,(extent[3]-bottom)*yscale)
    image=mosaic.transform(size,Image.Transform.EXTENT,crop,resample=Image.Resampling.BILINEAR)
    mosaic.close()
    return image,CREDIT+(' (some tiles unavailable)' if received<len(futures) else '')
