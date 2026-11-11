"""Render regional JPEGs from small province rasters, without a merged GIS export."""
import hashlib
import json
import io
import logging
import tempfile
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import shapely
from boto3.s3.transfer import TransferConfig
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling

from .export_styles import STYLES, classify, find_field

PIXELS = 1600
BATCH_SIZE = 256


def grid(bounds):
    left, bottom, right, top = bounds
    width, height = right-left, top-bottom
    if not np.isfinite(bounds).all() or width <= 0 or height <= 0:
        raise ValueError('Source has invalid map bounds.')
    columns = max(1, round(PIXELS * width / max(width, height)))
    rows = max(1, round(PIXELS * height / max(width, height)))
    return (rows, columns), from_bounds(*bounds, columns, rows)


def class_code(value):
    if isinstance(value, (int, float)) and pd.notna(value) and float(value).is_integer():
        value = str(int(value))
    return classify(value)


def province_raster(storage, bucket, source, layer, cache, boundary=None):
    identity = json.dumps(['raster-v1', layer, source['Key'], source.get('ETag'), str(source.get('LastModified', '')), PIXELS])
    if boundary is not None:
        fingerprint = hashlib.sha256(b''.join(boundary.to_crs(4326).geometry.to_wkb())).hexdigest()
        identity += '|clip-v1|' + fingerprint
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (hashlib.sha256(identity.encode()).hexdigest() + '.npz')
    if target.exists():
        with np.load(target, allow_pickle=False) as saved:
            return saved['pixels'], saved['bounds']
    remote_key = 'exports/regional-rasters/' + target.name
    persistent = hasattr(storage, 'put_object')
    if persistent:
        try:
            obj = storage.get_object(Bucket=bucket, Key=remote_key)
            try:
                payload = obj['Body'].read()
            finally:
                obj['Body'].close()
            with np.load(io.BytesIO(payload), allow_pickle=False) as saved:
                pixels, bounds = saved['pixels'], saved['bounds']
            target.write_bytes(payload)
            return pixels, bounds
        except Exception:
            # A missing or inaccessible derived cache must not block source downloads.
            pass
    palette = {code: i+1 for i, (code, _, _) in enumerate(STYLES[layer])}
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'source.gpkg'
        started = time.monotonic()
        print('REGIONAL_JPEG download', source['Key'], flush=True)
        transfer = TransferConfig(max_concurrency=2, max_io_queue=4, io_chunksize=256*1024)
        storage.download_file(bucket, source['Key'], str(path), Config=transfer)
        print('REGIONAL_JPEG downloaded', source['Key'], round(time.monotonic()-started, 2), 'seconds', flush=True)
        info = pyogrio.read_info(path)
        if not info['crs']:
            raise ValueError('Source data has no CRS.')
        field = find_field(pd.DataFrame(columns=info['fields']), layer)
        if not info['features']:
            raise ValueError('Source data has no features.')
        clip = boundary.to_crs(info['crs']) if boundary is not None else None
        native_bounds = tuple(clip.total_bounds) if clip is not None else info['total_bounds']
        bounds = np.array(Transformer.from_crs(info['crs'], 3857, always_xy=True).transform_bounds(*native_bounds, densify_pts=21))
        shape, transform = grid(bounds)
        tolerance = max(native_bounds[2]-native_bounds[0], native_bounds[3]-native_bounds[1]) / PIXELS / 4
        if clip is not None:
            clip = clip.copy()
            clip.geometry = shapely.make_valid(clip.geometry.array,method='structure',keep_collapsed=False)
            clip.geometry = clip.geometry.simplify(tolerance,preserve_topology=True)
        canvas = np.zeros(shape, dtype='uint8')
        for offset in range(0, info['features'], BATCH_SIZE):
            frame = gpd.read_file(path, columns=[field], bbox=native_bounds if clip is not None else None, skip_features=offset, max_features=BATCH_SIZE)
            if frame.empty:
                break
            frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty]
            frame = frame[frame.geom_type.isin(['Polygon', 'MultiPolygon'])].copy()
            # Quarter-pixel detail is sufficient for this display raster, before projection.
            frame.geometry = frame.geometry.simplify(tolerance, preserve_topology=True)
            if clip is not None and not frame.empty:
                invalid = ~frame.geometry.is_valid
                if invalid.any():
                    frame.loc[invalid, frame.geometry.name] = shapely.make_valid(frame.loc[invalid].geometry.array,method='structure',keep_collapsed=False)
                frame = gpd.clip(frame, clip, keep_geom_type=True)
            frame = frame.to_crs(3857)
            if frame.empty:
                continue
            values = frame[field].map(class_code).map(lambda code: palette.get(code,0))
            # GDAL burns only visible pixels; no merged full-detail geometry is retained.
            with rasterio.Env(GDAL_CACHEMAX=8*1024*1024):
                rasterize(zip(frame.geometry, values), out=canvas, transform=transform)
            del frame, values
            if offset % (BATCH_SIZE*40) == 0:
                print('REGIONAL_JPEG raster', source['Key'], offset, '/', info['features'], flush=True)
    print('REGIONAL_JPEG raster complete', source['Key'], round(time.monotonic()-started, 2), 'seconds', flush=True)
    staging = target.with_suffix('.part')
    with staging.open('wb') as stream:
        np.savez_compressed(stream, pixels=canvas, bounds=bounds)
    staging.replace(target)
    if persistent:
        try:
            storage.put_object(Bucket=bucket, Key=remote_key, Body=target.read_bytes(), ContentType='application/octet-stream')
        except Exception:
            logging.getLogger(__name__).warning('Regional raster retained locally; shared cache write unavailable')
    return canvas, bounds


def render_region(storage, bucket, sources, layer, title, target, cache, boundary=None):
    from .memory_utils import release_memory
    from .jpeg_layout import compose

    rasters = [province_raster(storage, bucket, source, layer, cache, boundary=boundary) for source in sources]
    bounds = (min(b[0] for _, b in rasters), min(b[1] for _, b in rasters), max(b[2] for _, b in rasters), max(b[3] for _, b in rasters))
    shape, transform = grid(bounds)
    canvas = np.zeros(shape, dtype='uint8')
    for pixels, source_bounds in rasters:
        projected = np.zeros(shape, dtype='uint8')
        reproject(pixels, projected,
                  src_transform=from_bounds(*source_bounds, pixels.shape[1], pixels.shape[0]), src_crs=3857,
                  dst_transform=transform, dst_crs=3857,
                  src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
        visible = projected != 0
        canvas[visible] = projected[visible]
    if not canvas.any():
        raise ValueError('No classified slope data is available in the selected area.')
    del rasters,projected,visible,pixels
    release_memory()
    return compose(canvas,bounds,layer,title,target,boundary_ha=float(boundary.to_crs(6933).geometry.area.sum()/10000) if boundary is not None else None)
