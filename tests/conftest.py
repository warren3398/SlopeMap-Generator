import pytest

@pytest.fixture(autouse=True)
def offline_hillshade(monkeypatch):
    from app import jpeg_layout
    monkeypatch.setattr(jpeg_layout,'hillshade_background',lambda bounds,size,**kwargs:(None,'Hillshade unavailable'))

    monkeypatch.setattr(jpeg_layout,'paint_ocean',lambda background,bounds:[])
