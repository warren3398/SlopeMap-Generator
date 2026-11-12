from PIL import Image,ImageColor
from shapely.geometry import box,Point
from shapely.strtree import STRtree
from rasterio.warp import transform_bounds
from app import coast


def fake_land(monkeypatch,geometry):
    monkeypatch.setattr(coast,'land_data',lambda:((geometry,),STRtree([geometry])))


def test_ocean_is_blue_but_land_without_hillshade_stays_gray(monkeypatch):
    fake_land(monkeypatch,box(0,0,.01,.01))
    bounds=transform_bounds(4326,3857,-.01,-.01,.02,.02)
    image=Image.new('RGB',(90,90),(240,240,240))
    shore=coast.paint_ocean(image,bounds)
    assert image.getpixel((5,5))==ImageColor.getrgb(coast.OCEAN)
    assert image.getpixel((45,45))==(240,240,240)
    coast.draw_shoreline(image,bounds,shore)
    assert image.getpixel((30,45))==ImageColor.getrgb(coast.SHORELINE)


def test_viewport_edges_are_not_drawn_as_shoreline(monkeypatch):
    fake_land(monkeypatch,box(-1,-1,1,1))
    bounds=transform_bounds(4326,3857,-.1,-.1,.1,.1)
    image=Image.new('RGB',(90,90),(240,240,240))
    shore=coast.paint_ocean(image,bounds)
    coast.draw_shoreline(image,bounds,shore)
    assert image.getpixel((0,45))==(240,240,240)
    assert image.getpixel((89,45))==(240,240,240)


def test_bundled_land_covers_camiguin_and_preserves_inland_lakes():
    geometries,tree=coast.land_data()
    assert all(geometry.is_valid and len(geometry.interiors)==0 for geometry in geometries)
    for point in [Point(124.73,9.17),Point(121.2,14.3)]:
        assert len(tree.query(point,predicate='intersects'))>0
    assert len(tree.query(Point(124.73,9.5),predicate='intersects'))==0
