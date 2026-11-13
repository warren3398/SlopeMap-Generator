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

class TransferProgressMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        from urllib.parse import parse_qs
        from . import transfer_progress as transfers
        token=parse_qs(scope.get('query_string',b'').decode()).get('transfer_id',[''])[0]
        if scope['type']!='http' or not scope.get('path','').startswith('/api/download-file/') or not transfers.valid(token):
            return await self.app(scope,receive,send)
        scope={**scope,'extensions':{k:v for k,v in scope.get('extensions',{}).items() if k!='http.response.pathsend'}}
        successful=False
        async def tracked(message):
            nonlocal successful
            if message['type']=='http.response.start':
                successful=message['status']==200
                headers=dict(message.get('headers',[]))
                transfers.start(token,headers.get(b'content-length'))
                if not successful:transfers.finish(token,failed=True)
            await send(message)
            if message['type']=='http.response.body' and successful:
                transfers.advance(token,len(message.get('body',b'')))
                if not message.get('more_body',False):transfers.finish(token)
        try:await self.app(scope,receive,tracked)
        except BaseException:
            transfers.finish(token,failed=True)
            raise

app.add_middleware(TransferProgressMiddleware)
