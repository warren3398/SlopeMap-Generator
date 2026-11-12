"""Offline land/ocean separation and shoreline drawing for the map viewport."""
from functools import lru_cache
import gzip
import json
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape,box
from shapely.strtree import STRtree
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from PIL import Image,ImageDraw

OCEAN='#dceff7'
SHORELINE='#376575'
CREDIT='Coastline: OCHA/NAMRIA via geoBoundaries; context: Natural Earth'
PROJECT=Transformer.from_crs(4326,3857,always_xy=True)


@lru_cache(maxsize=1)
def land_data():
    path=Path(__file__).with_name('data')/'land.geojson.gz'
    with gzip.open(path,'rt') as stream:data=json.load(stream)
    geometries=tuple(shape(feature['geometry']) for feature in data['features'])
    return geometries,STRtree(geometries)


def view_geometry(bounds):
    extent=box(*transform_bounds(3857,4326,*bounds,densify_pts=21))
    geometries,tree=land_data()
    land,shore=[],[]
    for index in tree.query(extent,predicate='intersects'):
        geometry=geometries[index]
        polygon=geometry.intersection(extent)
        if not polygon.is_empty:land.append(shapely.transform(polygon,PROJECT.transform,interleaved=False))
        # Clip original exteriors, never draw administrative or viewport-cut boundaries.
        line=geometry.exterior.intersection(extent)
        if not line.is_empty:shore.append(shapely.transform(line,PROJECT.transform,interleaved=False))
    return land,shore


def paint_ocean(image,bounds):
    land,shore=view_geometry(bounds)
    if land:
        mask=rasterize(((geometry,1) for geometry in land),out_shape=(image.height,image.width),transform=from_bounds(*bounds,image.width,image.height),fill=0,dtype='uint8')
    else:mask=np.zeros((image.height,image.width),dtype='uint8')
    ocean=Image.fromarray(np.where(mask==0,255,0).astype('uint8'))
    image.paste(OCEAN,(0,0,image.width,image.height),ocean)
    return shore


def draw_shoreline(image,bounds,shore,width=2):
    left,bottom,right,top=bounds
    xscale=image.width/(right-left);yscale=image.height/(top-bottom)
    draw=ImageDraw.Draw(image)
    for geometry in shore:
        for line in shapely.get_parts(geometry):
            if line.geom_type not in ('LineString','LinearRing'):continue
            points=[((x-left)*xscale,(top-y)*yscale) for x,y in line.coords]
            if len(points)>1:draw.line(points,fill=SHORELINE,width=width)
