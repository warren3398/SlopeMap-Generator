from io import BytesIO
import numpy as np
import pytest
from PIL import Image
from app import hillshade as basemap,jpeg_layout


def test_tile_mosaic_is_registered_and_north_is_up(monkeypatch):
    calls=[]
    def tile(z,x,y):
        calls.append((z,x,y));middle=2**z/2
        color=(200,0,0) if x<middle and y<middle else (0,200,0) if y<middle else (0,0,200) if x<middle else (200,200,0)
        return Image.new('RGB',(256,256),color)
    monkeypatch.setattr(basemap,'fetch_tile',tile)
    image,credit=basemap.background((-1000,-1000,1000,1000),(512,512))
    assert image.getpixel((20,20))==(200,0,0)
    assert image.getpixel((490,20))==(0,200,0)
    assert image.getpixel((20,490))==(0,0,200)
    assert image.getpixel((490,490))==(200,200,0)
    assert len(calls)<=basemap.MAX_TILES
    assert 'Esri' in credit and 'unavailable' not in credit


@pytest.mark.parametrize('bounds',[(-20037508,-20037508,20037508,20037508),(13000000,500000,14000000,2200000),(0,0,20,20)])
def test_tile_requests_stay_bounded(bounds):
    zoom,(x0,y0,x1,y1),extent=basemap.tile_plan(bounds,(1750,1370))
    assert (x1-x0+1)*(y1-y0+1)<=basemap.MAX_TILES
    assert 0<=zoom<=13
    assert extent[0]<=bounds[0] and extent[2]>=bounds[2]
    assert extent[1]<=bounds[1] and extent[3]>=bounds[3]


def test_provider_failure_returns_visible_status(monkeypatch):
    def unavailable(*args):raise OSError('offline')
    monkeypatch.setattr(basemap,'fetch_tile',unavailable)
    image,credit=basemap.background((-1000,-1000,1000,1000),(256,256))
    assert image is None and 'unavailable' in credit


def test_hillshade_tiles_are_cached(monkeypatch,tmp_path):
    payload=BytesIO();Image.new('RGB',(256,256),(20,40,60)).save(payload,format='JPEG')
    calls=[]
    def response(*args,**kwargs):calls.append(args[0].full_url);return BytesIO(payload.getvalue())
    monkeypatch.setattr(basemap,'CACHE',tmp_path)
    monkeypatch.setattr(basemap,'urlopen',response)
    first=basemap.fetch_tile(1,1,1);second=basemap.fetch_tile(1,1,1)
    assert first.size==second.size==(256,256) and len(calls)==1
    assert '/Elevation/World_Hillshade/' in calls[0]
    pixel=first.getpixel((100,100))
    assert pixel[0]==pixel[1]==pixel[2]


def test_overlay_is_translucent_and_nodata_shows_hillshade(monkeypatch,tmp_path):
    monkeypatch.setattr(jpeg_layout,'hillshade_background',lambda bounds,size:(Image.new('RGB',size,(20,40,60)),basemap.CREDIT))
    layer='slope'
    target=tmp_path/'basemap.jpg'
    jpeg_layout.compose(np.array([[1,0],[1,0]],dtype='uint8'),(0,0,1000,1000),layer,'Province: Test',target)
    with Image.open(target) as image:
        for corner in [(82,72),(2012,72),(82,1768),(2012,1768)]:
            assert np.max(np.abs(np.array(image.getpixel(corner))-[20,40,60]))<=3
        assert np.max(np.abs(np.array(image.getpixel((1500,700)))-[20,40,60]))<=3
        rgb=jpeg_layout.STYLES[layer][0][2]
        expected=np.array(rgb)*179/255+np.array([20,40,60])*76/255
        assert np.max(np.abs(np.array(image.getpixel((500,700)))-expected))<=4
