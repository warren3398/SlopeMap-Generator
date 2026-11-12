import pytest
from pyproj import Geod,Transformer
from app.jpeg_layout import scale_bar


@pytest.mark.parametrize('latitude,span,metres,label',[(16,100,20,'20 m'),(16,10000,2000,'2 km'),(60,1000000,100000,'100 km')])
def test_scale_bar_matches_ground_distance_and_units(latitude,span,metres,label):
    forward=Transformer.from_crs(4326,3857,always_xy=True)
    inverse=Transformer.from_crs(3857,4326,always_xy=True)
    x,y=forward.transform(121,latitude)
    bounds=(x-span/2,y-span/2,x+span/2,y+span/2)
    bar=scale_bar(bounds,1000)
    assert bar['metres']==metres
    assert bar['labels'][-1]==label
    assert 0<bar['pixels']<=320
    end_lon,end_lat=inverse.transform(x+bar['pixels']*span/1000,y)
    actual=Geod(ellps='WGS84').inv(121,latitude,end_lon,end_lat)[2]
    assert actual==pytest.approx(metres,rel=1e-8)
