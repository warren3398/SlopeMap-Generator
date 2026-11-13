import io,json,os,time,zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box,Polygon,Point
import shapely
from app import boundary_upload as upload

@pytest.fixture
def boundary(tmp_path,monkeypatch):
    monkeypatch.setattr(upload,'ROOT',tmp_path/'uploads')
    polygon=Polygon(box(120,16,120.1,16.1).exterior.coords,[box(120.03,16.03,120.05,16.05).exterior.coords])
    return gpd.GeoDataFrame({'ignore_this_attribute':['private']},geometry=[polygon],crs=4326)

@pytest.mark.parametrize('ext',['geojson','gpkg','zip'])
def test_formats_reproject_preserve_holes_and_strip_attributes(boundary,tmp_path,ext):
    frame=boundary.to_crs(3857)
    path=tmp_path/('input.'+('shp' if ext=='zip' else ext))
    frame.to_file(path,driver={'geojson':'GeoJSON','gpkg':'GPKG','zip':'ESRI Shapefile'}[ext])
    if ext=='zip':
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for p in tmp_path.glob('input.*'):z.write(p,'nested/'+p.name)
        raw=stream.getvalue()
    else:raw=path.read_bytes()
    meta=upload.save(raw,'my-boundary.'+ext)
    result,stored=upload.load(meta['boundary_id'])
    assert result.crs.to_epsg()==4326
    assert len(result.columns)==1
    assert result.geometry.iloc[0].symmetric_difference(boundary.geometry.iloc[0]).area<1e-10
    assert meta['area_ha']>0 and stored['name']=='my-boundary'

@pytest.mark.parametrize('geometry',[Point(120,16),Polygon()])
def test_nonpolygon_or_empty_is_rejected(boundary,geometry):
    raw=gpd.GeoDataFrame(geometry=[geometry],crs=4326).to_json().encode()
    with pytest.raises(ValueError):upload.save(raw,'wrong.geojson')


def test_limits_bad_zip_and_expired_handle(boundary,monkeypatch):
    with pytest.raises(ValueError,match='20 MB'):upload.save(b'x'*(upload.MAX_BYTES+1),'large.geojson')
    with pytest.raises(ValueError):upload.load('../../etc/passwd')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr('../escape.shp',b'invalid')
    with pytest.raises(ValueError,match='Unsafe'):upload.save(stream.getvalue(),'bad.zip')
    monkeypatch.setattr(upload,'MAX_COORDINATES',3)
    with pytest.raises(ValueError,match='vertices'):upload.save(boundary.to_json().encode(),'detailed.geojson')
    monkeypatch.setattr(upload,'MAX_COORDINATES',300000)
    meta=upload.save(boundary.to_json().encode(),'valid.geojson')
    os.utime(upload.ROOT/meta['boundary_id'],(time.time()-upload.TTL-1,)*2)
    with pytest.raises(ValueError,match='expired'):upload.load(meta['boundary_id'])


def test_missing_shapefile_crs(boundary,tmp_path):
    frame=boundary.copy();frame.crs=None
    frame.to_file(tmp_path/'missing.shp',driver='ESRI Shapefile')
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        for p in tmp_path.glob('missing.*'):z.write(p,p.name)
    meta=upload.save(buf.getvalue(),'missing.zip')
    assert meta['normalization']['assumed_crs']
    result,_=upload.load(meta['boundary_id'])
    assert result.geometry.iloc[0].symmetric_difference(boundary.geometry.iloc[0]).area<1e-10


def test_swapped_coordinates_are_corrected_automatically(boundary):
    wrong=boundary.copy()
    wrong.geometry=shapely.transform(wrong.geometry.array,lambda xy:xy[:,[1,0]])
    meta=upload.save(wrong.to_json().encode(),'swapped.geojson')
    result,_=upload.load(meta['boundary_id'])
    assert meta['normalization']['axis_swapped']
    assert result.geometry.iloc[0].symmetric_difference(boundary.geometry.iloc[0]).area<1e-10


def test_unlabelled_projected_boundary_requires_original_crs(boundary,tmp_path):
    frame=boundary.to_crs(3857).set_crs(None,allow_override=True)
    frame.to_file(tmp_path/'projected.shp',driver='ESRI Shapefile')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        for p in tmp_path.glob('projected.*'):z.write(p,p.name)
    raw=stream.getvalue()
    with pytest.raises(ValueError,match='cannot be guessed'):upload.save(raw,'projected.zip')
    meta=upload.save(raw,'projected.zip','EPSG:3857')
    result,_=upload.load(meta['boundary_id'])
    assert meta['normalization']['crs_override'] and meta['normalization']['reprojected']
    assert result.geometry.iloc[0].symmetric_difference(boundary.geometry.iloc[0]).area<1e-10


def test_reprojected_boundary_reports_alignment_metadata(boundary,tmp_path):
    path=tmp_path/'utm.gpkg'
    boundary.to_crs('ESRI:102457').to_file(path,driver='GPKG')
    meta=upload.save(path.read_bytes(),'utm.gpkg')
    assert meta['normalization']['reprojected']
    assert meta['normalization']['source_crs']=='ESRI:102457'
    assert meta['normalization']['target_crs']=='EPSG:4326'
