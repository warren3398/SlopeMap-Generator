from io import BytesIO
from pathlib import Path
import shutil
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from PIL import Image
from app import hillshade,jpeg_layout,downloader
from app.layout_units import px


class Store:
    def __init__(self):self.files={};self.versions={};self.ranges=[];self.downloads=[]
    def get_paginator(self,name):return self
    def paginate(self,Bucket,Prefix):
        return [{'Contents':[{'Key':key,'Size':path.stat().st_size,'ETag':self.versions.get(key,'v1'),'LastModified':'test'} for key,path in self.files.items() if key.startswith(Prefix)]}]
    def get_object(self,Bucket,Key,Range):
        self.ranges.append(Key)
        limit=int(Range.split('-')[-1])+1
        return {'Body':BytesIO(self.files[Key].read_bytes()[:limit])}
    def download_file(self,bucket,key,target,**kwargs):self.downloads.append(key);shutil.copyfile(self.files[key],target)


@pytest.fixture
def store(tmp_path,monkeypatch):
    storage=Store()
    monkeypatch.setattr(hillshade.r2gis,'client',lambda:storage)
    monkeypatch.setattr(downloader,'ROOT',tmp_path/'cache')
    return storage


def raster(store,tmp_path,name,bounds,value=100,crs='EPSG:3857',data=None):
    path=tmp_path/(name+'.tif')
    array=np.full((4,4),value,dtype='uint8') if data is None else data
    with rasterio.open(path,'w',driver='GTiff',width=array.shape[1],height=array.shape[0],count=1,dtype='uint8',nodata=255,crs=crs,transform=from_bounds(*bounds,array.shape[1],array.shape[0])) as output:output.write(array,1)
    key=hillshade.PREFIX+name+'_hillshade.tif';store.files[key]=path
    return key


def test_only_intersecting_provinces_download_and_neighbors_fill_frame(store,tmp_path):
    west=raster(store,tmp_path,'West',(0,0,400,400),100)
    east=raster(store,tmp_path,'East',(400,0,800,400),180)
    far=raster(store,tmp_path,'Far',(2000,2000,2400,2400),20)
    image,credit=hillshade.background((-100,-100,900,500),(1000,600))
    assert image.getpixel((250,200))==(100,100,100)
    assert image.getpixel((700,200))==(180,180,180)
    assert image.getpixel((20,20))==(240,240,240)
    assert set(store.downloads)=={west,east} and far in store.ranges
    assert 'uploaded provincial' in credit and 'Esri' not in credit


def test_nodata_does_not_overwrite_neighbors_and_black_is_valid(store,tmp_path):
    data=np.full((4,4),100,dtype='uint8');data[1,1]=255;data[2,2]=0
    raster(store,tmp_path,'A',(0,0,400,400),data=data)
    raster(store,tmp_path,'B',(0,0,400,400),180)
    image,_=hillshade.background((0,0,400,400),(4,4))
    assert image.getpixel((0,0))==(100,100,100)
    assert image.getpixel((1,1))==(180,180,180)
    assert image.getpixel((2,2))==(0,0,0)


@pytest.mark.parametrize('crs,bounds',[('EPSG:4326',(120,15,120.1,15.1)),('ESRI:102457',(679253,1004074,700505,1023902))])
def test_raster_native_crs_is_reprojected_to_map(store,tmp_path,crs,bounds):
    raster(store,tmp_path,'Native',bounds,80,crs=crs)
    source=hillshade.catalog()[0]
    info=hillshade.metadata(source)
    assert hillshade.cache_paths(source)==hillshade.cache_paths(info)
    expected=transform_bounds(crs,'EPSG:3857',*bounds,densify_pts=21)
    assert info['bounds']==pytest.approx(expected)
    image,_=hillshade.background(expected,(32,32))
    assert image.getpixel((16,16))==(80,80,80)


def test_file_and_header_cache_are_invalidated_by_etag(store,tmp_path):
    key=raster(store,tmp_path,'Versioned',(0,0,400,400),90)
    first=hillshade.catalog()[0]
    image,_=hillshade.background((0,0,400,400),(4,4))
    ranges=len(store.ranges)
    hillshade.background((0,0,400,400),(4,4))
    assert len(store.ranges)==ranges and store.downloads==[key]
    store.versions[key]='v2'
    raster(store,tmp_path,'Versioned',(0,0,400,400),210)
    second=hillshade.catalog()[0]
    image,_=hillshade.background((0,0,400,400),(4,4))
    assert hillshade.cache_paths(first)!=hillshade.cache_paths(second)
    assert image.getpixel((1,1))==(210,210,210)
    assert store.downloads==[key,key]


def test_missing_coverage_is_reported_without_other_basemap(store,tmp_path):
    raster(store,tmp_path,'Far',(2000,2000,2400,2400),20)
    image,credit=hillshade.background((0,0,100,100),(4,4))
    assert image is None and 'does not cover' in credit and store.downloads==[]


def test_overlay_is_translucent_and_full_frame_has_hillshade(monkeypatch,tmp_path):
    monkeypatch.setattr(jpeg_layout,'hillshade_background',lambda bounds,size,**kwargs:(Image.new('RGB',size,(40,40,40)),hillshade.CREDIT))
    target=tmp_path/'hillshade.jpg'
    jpeg_layout.compose(np.array([[1,0],[1,0]],dtype='uint8'),(0,0,1000,1000),'slope','Province: Test',target)
    with Image.open(target) as image:
        for point in [(82,72),(2012,72),(82,1768),(2012,1768),(1500,700)]:
            assert np.max(np.abs(np.array(image.getpixel(tuple(px(v) for v in point)))-[40,40,40]))<=3
        rgb=jpeg_layout.STYLES['slope'][0][2]
        expected=np.array(rgb)*179/255+np.array([40,40,40])*76/255
        assert np.max(np.abs(np.array(image.getpixel((px(500),px(700))))-expected))<=4
