import io
import geopandas as gpd
import pytest
from PIL import Image
from shapely.geometry import box,Polygon
import shapely
from app import boundary_upload as upload,slope_sources
from test_downloader import setup,ready

@pytest.mark.parametrize('fmt',['jpeg','zip','gpkg'])
def test_custom_boundary_crossing_provinces_preserves_hole(setup,tmp_path,monkeypatch,fmt):
    client,_,_,storage=setup
    monkeypatch.setattr(upload,'ROOT',tmp_path/'uploads')
    mask=Polygon(box(120.02,16.02,120.18,16.08).exterior.coords,[box(120.08,16.04,120.12,16.06).exterior.coords])
    frame=gpd.GeoDataFrame(geometry=[mask],crs=4326)
    assert len(slope_sources.select_boundary_sources(frame))==2
    response=client.post('/api/upload-boundary',files={'boundary':('test-boundary.geojson',frame.to_json().encode(),'application/geo+json')})
    assert response.status_code==200,response.text
    token=response.json()['boundary_id']
    state=ready(client,{'boundary_id':token,'format':fmt})
    assert state['status']=='ready',state
    response=client.get(state['download_url']);assert response.status_code==200
    if fmt=='jpeg':
        assert Image.open(io.BytesIO(response.content)).size==(3840,2406)
        import numpy as np
        from pyproj import Transformer
        x,y=Transformer.from_crs(4326,3857,always_xy=True).transform(120.1,16.05)
        for cache in (upload.ROOT/token).rglob('*.npz'):
            with np.load(cache) as raster:
                pixels,bounds=raster['pixels'],raster['bounds']
                row=int((bounds[3]-y)/(bounds[3]-bounds[1])*pixels.shape[0])
                col=int((x-bounds[0])/(bounds[2]-bounds[0])*pixels.shape[1])
                assert pixels[row,col]==0,'JPEG must preserve the uploaded polygon hole'
    else:
        path=tmp_path/('custom.'+fmt);path.write_bytes(response.content)
        result=gpd.read_file('zip://'+str(path) if fmt=='zip' else path)
        assert shapely.union_all(result.geometry).symmetric_difference(mask).area<1e-10
        assert (result.clip_ha>0).all()
    assert not storage.objects,'Custom-boundary rasters and exports must stay out of shared storage'


def test_invalid_upload_and_missing_handle_do_not_start_job(setup):
    client,_,_,_=setup
    response=client.post('/api/upload-boundary',files={'boundary':('bad.geojson',b'bad')})
    assert response.status_code==400
    response=client.post('/api/prepare-download',json={'boundary_id':'a'*32,'format':'jpeg'})
    assert response.status_code==400 and 'Upload' in response.json()['detail']


def test_stage_and_transfer_progress_follow_real_job_and_file(setup):
    client,_,_,_=setup
    state=ready(client,{'level':'province','fid':'1','format':'gpkg'})
    assert state['status']=='ready' and state['progress']==100
    assert state['progress_kind']=='stage-estimate'
    response=client.get(state['download_url']+'?transfer_id='+'b'*32)
    transfer=client.get('/api/transfer-progress/'+'b'*32).json()
    assert transfer['status']=='complete' and transfer['percent']==100
    assert transfer['bytes_sent']==transfer['total_bytes']==len(response.content)


def test_asgi_transfer_exposes_partial_bytes_and_disconnect():
    import asyncio
    from app.main import TransferProgressMiddleware
    from app import transfer_progress
    token='c'*32
    async def stream(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-length',b'6')]})
        await send({'type':'http.response.body','body':b'abc','more_body':True})
        assert transfer_progress.state(token)['percent']==50
        await send({'type':'http.response.body','body':b'def','more_body':False})
    async def send(message):pass
    scope={'type':'http','path':'/api/download-file/test','query_string':('transfer_id='+token).encode()}
    asyncio.run(TransferProgressMiddleware(stream)(scope,None,send))
    assert transfer_progress.state(token)['percent']==100
    async def disconnect(message):
        if message['type']=='http.response.body':raise OSError('client disconnected')
    with pytest.raises(OSError):asyncio.run(TransferProgressMiddleware(stream)(scope,None,disconnect))
    assert transfer_progress.state(token)['status']=='failed'
