from app.jpeg_layout import map_view,MAP_SIZE
import pytest

@pytest.mark.parametrize('shape',[(1600,300),(300,1600),(800,800)])
def test_full_frame_preserves_overlay_registration_and_scale(shape):
    bounds=(13000000,1700000,13024000,1740000)
    view,size,offset=map_view(bounds,shape)
    xstep=(view[2]-view[0])/MAP_SIZE[0]
    ystep=(view[3]-view[1])/MAP_SIZE[1]
    assert view[0]+offset[0]*xstep==pytest.approx(bounds[0])
    assert view[3]-offset[1]*ystep==pytest.approx(bounds[3])
    assert view[0]+(offset[0]+size[0])*xstep==pytest.approx(bounds[2])
    assert view[3]-(offset[1]+size[1])*ystep==pytest.approx(bounds[1])
    assert xstep==pytest.approx((bounds[2]-bounds[0])/size[0])
    assert ystep==pytest.approx((bounds[3]-bounds[1])/size[1])
