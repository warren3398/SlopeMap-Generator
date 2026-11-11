from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from .r2gis import router
app=FastAPI(title="SlopeMap Generator",version="0.2.0")
app.include_router(router)
from .downloader import router as download_router
app.include_router(download_router)
@app.get("/",response_class=HTMLResponse)
def home():
    return Path("static/index.html").read_text(encoding="utf-8")
@app.get("/health")
def health():
    from .r2gis import configured
    return {"status":"ok","storage":"r2","r2_configured":configured()}
