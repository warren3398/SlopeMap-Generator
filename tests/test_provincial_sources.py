import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box
from pyproj import Transformer

from app import downloader as service
from app import slope_sources as sources
from test_downloader import setup,ready


def add_palawan(setup,tmp_path,duplicate=True):
    client,paths,versions,storage=setup
    provinces=gpd.read_file(paths['province'])
    provinces.loc[len(provinces)]=['Palawan','PH17',box(118,8,120,12)]
    paths['province'].unlink();provinces.to_file(paths['province'],driver='GPKG')
    service.attributes.cache_clear()
    north=tmp_path/'north.gpkg';south=tmp_path/'south.gpkg'
    gpd.GeoDataFrame({'Slope_Class':['F','A']},geometry=[box(118,10,120,12),box(119,9,119.1,9.1)],crs=4326).to_file(north,driver='GPKG')
    geometry=[box(118,8,120,10.5)];codes=['A']
    if duplicate:geometry.append(box(118,10,120,12));codes.append('F')
    gpd.GeoDataFrame({'Slope_Class':codes},geometry=geometry,crs=4326).to_file(south,driver='GPKG')
    storage.files['slope/Palawan_slope_north.gpkg']=north
    storage.files['slope/Palawan_slope_south.gpkg']=south
    return storage


def test_selects_only_the_requested_provincial_packages(setup,tmp_path):
    storage=add_palawan(setup,tmp_path)
    selected=sources.select_sources('municipality','1')
    assert [item['Key'] for item in selected]==['slope/Abra_slope.gpkg']
    region=sources.select_sources('region','140000000')
    assert {item['Key'] for item in region}=={'slope/Abra_slope.gpkg','slope/Benguet_slope.gpkg'}
    palawan=sources.select_sources('province','3')
    assert {item['part'] for item in palawan}=={'north','south'}


def test_palawan_requires_both_parts(setup,tmp_path):
    storage=add_palawan(setup,tmp_path)
    del storage.files['slope/Palawan_slope_south.gpkg']
    with pytest.raises(ValueError,match='north and south'):sources.select_sources('province','3')


def test_palawan_vector_merges_parts_without_duplicate_features_or_overlap(setup,tmp_path):
    client,_,_,_=setup
    add_palawan(setup,tmp_path)
    state=ready(client,dict(level='province',fid='3',format='gpkg'))
    assert state['status']=='ready',state
    response=client.get(state['download_url'])
    output=tmp_path/'palawan.gpkg';output.write_bytes(response.content)
    frame=gpd.read_file(output)
    assert len(frame)==3
    assert set(frame.source_part)=={'north','south'}
    assert set(frame.source_key)=={'slope/Palawan_slope_north.gpkg','slope/Palawan_slope_south.gpkg'}
    assert all(geometry.is_valid for geometry in frame.geometry)
    assert sum(geometry.area for geometry in frame.geometry)==pytest.approx(8)
    assert frame.geometry.union_all().equals(box(118,8,120,12))
    for first in range(len(frame)):
        for second in range(first):assert frame.geometry.iloc[first].intersection(frame.geometry.iloc[second]).area==pytest.approx(0)
    expected=gpd.GeoSeries([box(118,8,120,12)],crs=4326).to_crs(6933).area.iloc[0]/10000
    assert frame.clip_ha.sum()==pytest.approx(expected,rel=1e-8)


def test_palawan_raster_prefers_first_part_in_overlap(setup,tmp_path,monkeypatch):
    from app import jpeg_layout,raster_exports
    storage=add_palawan(setup,tmp_path,duplicate=False)
    boundary,title=service.selection_boundary('province','3')
    selected=sources.select_sources('province','3')
    captured={}
    def capture(canvas,bounds,*args,**kwargs):captured.update(canvas=canvas,bounds=bounds);return {}
    monkeypatch.setattr(jpeg_layout,'compose',capture)
    raster_exports.render_region(service.LocalSource(selected),'data',selected,'slope',title,tmp_path/'unused.jpg',service.ROOT/'rasters',boundary=boundary)
    canvas,bounds=captured['canvas'],captured['bounds']
    x,y=Transformer.from_crs(4326,3857,always_xy=True).transform(119,10.25)
    row=int((bounds[3]-y)/(bounds[3]-bounds[1])*canvas.shape[0]);column=int((x-bounds[0])/(bounds[2]-bounds[0])*canvas.shape[1])
    assert canvas[row,column]==6


def test_uploaded_file_availability_replaces_retired_master_extent_check(setup,tmp_path):
    client,paths,_,storage=setup
    municipalities=gpd.read_file(paths['municipality'])
    municipalities.loc[len(municipalities)]=['Nueva Vizcaya','Ambaguio','PHL.NV.1',box(121,16.3,121.1,16.4)]
    paths['municipality'].unlink();municipalities.to_file(paths['municipality'],driver='GPKG')
    service.attributes.cache_clear()
    data=tmp_path/'nueva.gpkg'
    gpd.GeoDataFrame({'Slope_Class':['C']},geometry=[box(121,16.3,121.1,16.4)],crs=4326).to_file(data,driver='GPKG')
    storage.files['slope/Nueva_Vizcaya_slope.gpkg']=data
    response=client.get('/api/slope-coverage?level=municipality&fid=3')
    assert response.status_code==200 and response.json()['status']=='possible'
    assert [item['Key'] for item in sources.select_sources('municipality','3')]==['slope/Nueva_Vizcaya_slope.gpkg']
    result=ready(client,dict(level='municipality',fid='3',format='gpkg'))
    assert result['status']=='ready',result


def test_jpeg_skips_parts_outside_selected_municipality(setup,tmp_path,monkeypatch):
    from app import raster_exports,jpeg_layout
    storage=add_palawan(setup,tmp_path,duplicate=False)
    boundary=gpd.GeoDataFrame(geometry=[box(118,11,120,12)],crs=4326)
    selected=sources.select_sources('province','3')
    monkeypatch.setattr(jpeg_layout,'compose',lambda canvas,*args,**kwargs: {'pixels':int(np.count_nonzero(canvas))})
    result=raster_exports.render_region(service.LocalSource(selected),'data',selected,'slope','North municipality',tmp_path/'unused.jpg',service.ROOT/'rasters',boundary=boundary)
    assert result['pixels']>0


def test_large_jpeg_sources_are_simplified_by_gdal_before_loading(setup,tmp_path,monkeypatch):
    from app import raster_exports,jpeg_layout
    add_palawan(setup,tmp_path,duplicate=False)
    selected=sources.select_sources('province','3')
    for source in selected:source['Size']=200*1024*1024
    boundary,title=service.selection_boundary('province','3')
    monkeypatch.setattr(jpeg_layout,'compose',lambda canvas,*args,**kwargs:{'pixels':int(np.count_nonzero(canvas))})
    result=raster_exports.render_region(service.LocalSource(selected),'data',selected,'slope',title,tmp_path/'unused.jpg',service.ROOT/'rasters',boundary=boundary)
    assert result['pixels']>0


def test_jpeg_masks_pixels_outside_nonrectangular_boundary(setup,tmp_path):
    from app import raster_exports
    from shapely.geometry import Polygon
    client,paths,_,storage=setup
    boundary=gpd.GeoDataFrame(geometry=[Polygon([(120,16),(120.2,16),(120,16.2),(120,16)])],crs=4326)
    selected=sources.select_sources('province','1')[0]
    pixels,bounds=raster_exports.province_raster(service.LocalSource([selected]),'data',selected,'slope',service.ROOT/'rasters',boundary=boundary)
    assert pixels.any()
    x,y=Transformer.from_crs(4326,3857,always_xy=True).transform(120.17,16.17)
    row=int((bounds[3]-y)/(bounds[3]-bounds[1])*pixels.shape[0]);column=int((x-bounds[0])/(bounds[2]-bounds[0])*pixels.shape[1])
    assert pixels[row,column]==0
