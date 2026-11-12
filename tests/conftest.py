import pytest

@pytest.fixture(autouse=True)
def offline_satellite(monkeypatch):
    from app import jpeg_layout
    monkeypatch.setattr(jpeg_layout,'satellite_background',lambda bounds,size:(None,'Satellite imagery unavailable'))
