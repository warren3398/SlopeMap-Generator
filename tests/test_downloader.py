import io
import time
import shutil
from pathlib import Path

import geopandas as gpd
import pytest
from PIL import Image
from shapely.geometry import box
from fastapi.testclient import TestClient

from app import downloader as service
from app.main import app

class Body(io.BytesIO):
    def iter_chunks(self,chunk_size):
        while chunk:=self.read(chunk_size):yield chunk

class Storage:
    def __init__(self):self.objects={};self.files={};self.versions={}
    def get_object(self,Bucket,Key,**kwargs):return {'Body':Body(self.objects[Key])}
    def put_object(self,Bucket,Key,Body,**kwargs):self.objects[Key]=Body
    def upload_file(self,path,bucket,key,**kwargs):self.objects[key]=Path(path).read_bytes()
    def get_paginator(self,name):return self
    def paginate(self,Bucket,Prefix):
        return [{'Contents':[{'Key':key,'Size':path.stat().st_size,'ETag':self.versions.get('slope','test'),'LastModified':'test'} for key,path in sorted(self.files.items()) if key.startswith(Prefix)]}]
    def download_file(self,bucket,key,target,**kwargs):shutil.copyfile(self.files[key],target)

@pytest.fixture
def setup(tmp_path,monkeypatch):
    paths={}
    records={
      'province':({'ADM2_EN':['Abra','Benguet'],'ADM1_PCODE':['PH14','PH14']},[box(120,16,120.1,16.1),box(120.1,16,120.2,16.1)]),
      'municipality':({'NAME_1':['Abra','Benguet'],'NAME_2':['Bangued','Bokod'],'GID_2':['PHL.1.1','PHL.2.1']},[box(120,16,120.05,16.1),box(120.1,16,120.15,16.1)]),
      'barangay':({'NAME_1':['Abra','Benguet'],'NAME_2':['Bangued','Bokod'],'NAME_3':['Agtangao','Other'],'GID_2':['PHL.1.1','PHL.2.1']},[box(120,16,120.025,16.1),box(120.1,16,120.125,16.1)]),
      'slope':({'Slope_Class':['A','F'],'Area_Ha':[999.,999.]},[box(120,16,120.1,16.1),box(120.1,16,120.2,16.1)]),
    }
    for kind,(data,geometry) in records.items():
        paths[kind]=tmp_path/(kind+'.gpkg')
        gpd.GeoDataFrame(data,geometry=geometry,crs=4326).to_file(paths[kind],driver='GPKG')
    versions={kind:kind+'-test' for kind in paths}
    def object_file(kind):return paths[kind],{'Key':service.r2gis.KEYS[kind],'ETag':versions[kind],'LastModified':'test'}
    storage=Storage()
    storage.files={'slope/Abra_slope.gpkg':paths['slope'],'slope/Benguet_slope.gpkg':paths['slope']}
    storage.versions=versions
    monkeypatch.setattr(service,'ROOT',tmp_path/'cache')
    monkeypatch.setattr(service,'object_file',object_file)
    monkeypatch.setattr(service.r2gis,'client',lambda:storage)
    service.JOBS.clear();service.attributes.cache_clear()
    with TestClient(app) as client:yield client,paths,versions,storage


def ready(client,params):
    response=client.post('/api/prepare-download',json=params)
    assert response.status_code==200,response.text
    for _ in range(500):
        state=client.get('/api/download-status/'+response.json()['job_id']).json()
        if state['status'] in ('ready','failed'):return state
        time.sleep(.01)
    raise AssertionError('Preparation did not finish')


def test_dropdowns_use_real_parent_relationships_and_region_membership(setup):
    client,_,_,_=setup
    regions=client.get('/api/slope-boundaries?level=region').json()
    assert len(regions)==18 and regions[1]['island']=='Luzon'
    provinces=client.get('/api/slope-boundaries?level=province&parent_code=region:140000000').json()
    assert [row['name'] for row in provinces]==['Abra','Benguet']
    municipalities=client.get('/api/slope-boundaries?level=municipality&parent_code=province:1').json()
    assert [row['name'] for row in municipalities]==['Bangued']
    barangays=client.get('/api/slope-boundaries?level=barangay&parent_code=municipality:1').json()
    assert [row['name'] for row in barangays]==['Agtangao']

@pytest.mark.parametrize('level,fid',[('region','140000000'),('province','1'),('municipality','1'),('barangay','1')])
@pytest.mark.parametrize('fmt',['jpeg','zip','gpkg'])
def test_all_levels_and_formats_are_direct_downloads(setup,tmp_path,level,fid,fmt):
    client,_,_,_=setup
    state=ready(client,dict(level=level,fid=fid,format=fmt))
    assert state['status']=='ready',state
    response=client.get(state['download_url'])
    assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
    if fmt=='jpeg':
        image=Image.open(io.BytesIO(response.content));image.verify()
        assert image.size==(3840,2406)
        return
    target=tmp_path/('download.'+fmt);target.write_bytes(response.content)
    frame=gpd.read_file('zip://'+str(target) if fmt=='zip' else target)
    assert len(frame)==(2 if level=='region' else 1)
    assert frame.is_valid.all() and (frame.clip_ha>0).all()
    assert set(frame.slope_code)<={'A','F'}
    width={'region':.2,'province':.1,'municipality':.05,'barangay':.025}[level]
    assert frame.total_bounds[2]==pytest.approx(120+width)

@pytest.mark.parametrize('fmt',['jpeg','gpkg','zip'])
def test_incomplete_source_coverage_is_not_reported_as_ready(setup,fmt):
    client,paths,versions,_=setup
    paths['slope'].unlink()
    gpd.GeoDataFrame({'Slope_Class':['A']},geometry=[box(120,16,120.005,16.1)],crs=4326).to_file(paths['slope'],driver='GPKG')
    versions['slope']='partial'
    state=ready(client,dict(level='province',fid='1',format=fmt))
    assert state['status']=='failed'
    assert 'covers only' in state['error']


def test_shared_cache_restores_file_without_repeating_clipping(setup,monkeypatch):
    client,_,_,storage=setup
    params=dict(level='municipality',fid='1',format='gpkg')
    first=ready(client,params);assert first['status']=='ready'
    shutil.rmtree(service.ROOT/'exports')
    import app.vector_exports as vectors
    def fail(*args,**kwargs):raise AssertionError('Prepared file must be restored from shared cache')
    monkeypatch.setattr(vectors,'build_geopackage',fail)
    second=ready(client,params);assert second['status']=='ready',second
    assert client.get(second['download_url']).content==client.get(first['download_url']).content


def test_invalid_selection_and_lost_job_report_clear_errors(setup):
    client,_,_,_=setup
    assert client.post('/api/prepare-download',json={'level':'invalid','fid':'1','format':'jpeg'}).status_code==400
    response=client.get('/api/download-status/missing')
    assert response.status_code==404 and 'server restart' in response.json()['detail']


def test_source_version_changes_invalidate_completed_artifacts(setup):
    client,_,versions,_=setup
    params=dict(level='municipality',fid='1',format='gpkg')
    first=ready(client,params);assert first['status']=='ready'
    old=service.JOBS[first['download_url'].split('/')[-1]]['path']
    versions['slope']='updated-source'
    second=ready(client,params);assert second['status']=='ready'
    new=service.JOBS[second['download_url'].split('/')[-1]]['path']
    assert new!=old


def test_preparation_deduplicates_requests_without_blocking_http(setup,monkeypatch,tmp_path):
    import threading
    client,_,_,_=setup
    entered=threading.Event();release=threading.Event()
    def slow(params):
        entered.set();release.wait(3)
        path=tmp_path/'test.jpg';path.write_bytes(b'test')
        return path,'test.jpg'
    monkeypatch.setattr(service,'prepare_file',slow)
    params=dict(level='province',fid='1',format='jpeg')
    try:
        started=time.monotonic();first=client.post('/api/prepare-download',json=params)
        assert time.monotonic()-started<1
        assert entered.wait(1)
        second=client.post('/api/prepare-download',json=params)
        assert first.json()['job_id']==second.json()['job_id']
    finally:release.set()
    for _ in range(100):
        state=client.get('/api/download-status/'+first.json()['job_id']).json()
        if state['status']=='ready':break
        time.sleep(.01)
    assert state['status']=='ready'


def test_invalid_polygons_are_repaired_for_vector_clipping(setup,tmp_path):
    from shapely.geometry import Polygon
    from app.vector_exports import build_geopackage
    client,paths,versions,_=setup
    paths['slope'].unlink()
    polygon=Polygon([(120,16),(120.1,16.1),(120,16.1),(120.1,16),(120,16)])
    gpd.GeoDataFrame({'Slope_Class':['A']},geometry=[polygon],crs=4326).to_file(paths['slope'],driver='GPKG')
    boundary,_=service.selection_boundary('province','1')
    target=tmp_path/'repaired.gpkg'
    metadata=build_geopackage(service.LocalSource([{'Key':'slope/Abra_slope.gpkg'}]),'data',[{'Key':'slope/Abra_slope.gpkg'}],'slope',boundary,target)
    frame=gpd.read_file(target)
    assert metadata['feature_count']==1 and metadata['total_ha']>0
    assert frame.is_valid.all()


def test_partial_coverage_is_reported_with_ready_download(setup):
    client,paths,versions,_=setup
    paths['slope'].unlink()
    gpd.GeoDataFrame({'Slope_Class':['A']},geometry=[box(120,16,120.06,16.1)],crs=4326).to_file(paths['slope'],driver='GPKG')
    versions['slope']='sixty-percent'
    state=ready(client,dict(level='province',fid='1',format='gpkg'))
    assert state['status']=='ready',state
    assert 59<state['coverage_pct']<61


def test_independent_city_names_match_the_shared_region_catalog():
    assert service.canonical('City of Cotabato')=='cotabato-city'
    assert service.canonical('City of Isabela')=='isabela-city'


def test_uploaded_hillshade_update_invalidates_jpeg_but_reuses_classification_raster(setup,monkeypatch):
    from app import hillshade
    client,_,_,_=setup
    source={'Key':'hillshade/provinces/Abra_hillshade.tif','Size':100,'ETag':'first','LastModified':'test'}
    monkeypatch.setattr(hillshade,'catalog',lambda:[dict(source)])
    params=dict(level='municipality',fid='1',format='jpeg')
    first=ready(client,params)
    first_path=Path(service.JOBS[first['download_url'].split('/')[-1]]['path'])
    rasters=list((service.ROOT/'rasters').glob('*.npz'))
    source['ETag']='updated'
    second=ready(client,params)
    second_path=Path(service.JOBS[second['download_url'].split('/')[-1]]['path'])
    assert first_path.parent!=second_path.parent
    assert rasters==list((service.ROOT/'rasters').glob('*.npz'))
