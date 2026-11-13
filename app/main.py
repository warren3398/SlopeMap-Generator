from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse,JSONResponse
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

@app.middleware('http')
async def limit_boundary_upload(request,call_next):
    if request.url.path=='/api/upload-boundary':
        length=request.headers.get('content-length','')
        if length.isdigit() and int(length)>21*1024*1024:
            return JSONResponse({'detail':'Boundary upload must be 20 MB or smaller.'},status_code=413)
    return await call_next(request)
