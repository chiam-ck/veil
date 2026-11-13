import json
import os
import shutil
import tempfile
import zipfile
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Annotated
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from . import config, store
from . import __version__
from .llm import detector_config
from .provider_health import public_status
from . import workspaces
Owner = Annotated[str,Depends(workspaces.owner)]

@asynccontextmanager
async def lifespan(app):
    store.init()
    yield

app=FastAPI(title="Veil document redaction",version=__version__,lifespan=lifespan,docs_url=None,redoc_url=None)

class BoundedMultipartRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def bounded(request):
            form=None
            try:
                if request.headers.get('content-type','').split(';',1)[0].strip().lower()=='multipart/form-data':
                    form=await request.form(max_files=1,max_fields=2,max_part_size=4096)
                return await handler(request)
            finally:
                if form is not None:await form.close()
        return bounded

app.router.route_class=BoundedMultipartRoute

def secure_response(request,response):
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
        'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
    if request.url.scheme=='https':response.headers['Strict-Transport-Security']='max-age=31536000'
    return response

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Validation input may contain a pasted API key or sensitive test text.
    return JSONResponse({'detail':[{'loc':e['loc'],'msg':e['msg'],'type':e['type']} for e in exc.errors()]},status_code=422)

class BodyLimit:
    def __init__(self, app): self.app=app
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope,receive,send)
        maximum=config.MAX_BYTES+65536
        headers=dict(scope.get("headers",[]))
        try: declared=int(headers.get(b"content-length",b"0"))
        except ValueError: declared=maximum+1
        if declared<0 or declared>maximum:
            return await JSONResponse({"detail":"Request body limit exceeded"},status_code=413)(scope,receive,send)
        total=0
        async def bounded():
            nonlocal total
            message=await receive();total+=len(message.get("body",b""))
            if total>maximum: raise HTTPException(413,"Request body limit exceeded")
            return message
        await self.app(scope,bounded,send)

app.add_middleware(BodyLimit)

@app.middleware("http")
async def deployment_security(request:Request,call_next):
    def deny(status,message):return secure_response(request,JSONResponse({'detail':message},status_code=status))
    try:
        value=request.headers.get('host','')
        parts=urlsplit('//'+value)
        port=parts.port
        host=parts.hostname
        if (len(request.headers.getlist('host'))!=1 or parts.username or parts.password or parts.path
                or parts.query or parts.fragment or any(c.isspace() for c in value)):
            host=None
    except ValueError:host=None
    if host is None or host.lower() not in config.ALLOWED_HOSTS:return deny(400,'Untrusted Host header')
    origin=request.headers.get("origin")
    if origin and origin != request.url.scheme+"://"+request.headers.get("host",""):
        return deny(403,'Cross-origin requests are not allowed')
    private=request.url.path.startswith(('/api/jobs','/api/batches','/api/workspace'))
    if private:
        try:workspaces.require_transport(request)
        except HTTPException as error:return deny(error.status_code,error.detail)
        if request.headers.get('sec-fetch-site','none') not in {'none','same-origin'}:
            return deny(403,'Cross-origin requests are not allowed')
        if request.method not in {'GET','HEAD','OPTIONS'} and request.headers.get('x-veil-request')!='1':
            return deny(403,'Missing same-origin request header')
    response=await call_next(request)
    refresh=getattr(request.state,'refresh_workspace_cookie',None)
    if refresh and request.url.path!='/api/workspace/end' and workspaces.resolve(request,touch=False):
        workspaces.set_cookie(request,response,*refresh)
    return secure_response(request,response)

@app.exception_handler(KeyError)
async def not_found(request,exc): return JSONResponse({"detail":"Job not found or invalid ID"},status_code=404)

@app.get("/api/health")
def health(): return {"status":"ok","version":__version__,"policy":config.POLICY,"device":"CPU","detector":detector_config(),"provider":public_status(),"warning":"Completion is not a guarantee of zero PII; LLM mode sends extracted text to the configured provider"}

@app.get('/api/llm/config')
def llm_config():
    return detector_config()

@app.get('/api/provider/status')
def provider_status():
    return public_status()

@app.get('/api/workspace')
def workspace(request:Request,response:Response):
    return workspaces.bootstrap(request,response)

@app.post('/api/workspace/end',status_code=204)
def end_workspace(request:Request,response:Response,owner:Owner):
    workspaces.end(request,response,owner)

@app.get('/workspace-ca.crt')
def workspace_ca():
    certificate=config.DATA/'tls'/'ca.crt'
    if not certificate.is_file():raise HTTPException(404,'Certificate unavailable')
    return FileResponse(certificate,filename='veil-workspace-ca.crt',media_type='application/x-x509-ca-cert')

@app.post("/api/jobs",status_code=202)
async def submit(owner:Owner,file:UploadFile=File(...),batch_id:str|None=Form(None),batch_index:int|None=Form(None)):
    suffix=Path(file.filename or "").suffix.lower()
    if suffix not in config.SUPPORTED:
        raise HTTPException(415,"Supported: DOCX, PPTX, XLSX, PDF. Convert legacy DOC/PPT/XLS to supported formats first.")
    try: jid=store.reserve(suffix,batch_id,batch_index,owner)
    except OverflowError: raise HTTPException(429,"Queue capacity reached; upload will be retried when capacity is available",headers={"Retry-After":"2"})
    except store.BatchConflict as e:raise HTTPException(409,str(e))
    except ValueError as e:raise HTTPException(400,str(e))
    try:
        size=0
        with (store.path(jid)/"original"/("source"+suffix)).open("xb") as f:
            while data:=await file.read(1024*1024):
                size+=len(data)
                if size>config.MAX_BYTES: raise HTTPException(413,"File size limit exceeded")
                f.write(data)
        if not size: raise HTTPException(400,"Empty file")
        store.update(jid,"queued","waiting for worker")
    except BaseException:
        store.update(jid,"failed","upload interrupted")
        store.delete(jid,release_batch_slot=True)
        raise
    finally: await file.close()
    return {"id":jid,"status":"queued"}

@app.get("/api/jobs/{jid}")
def status(jid:str,owner:Owner): return store.get(jid,owner)

@app.get("/api/jobs/{jid}/report")
def report(jid:str,owner:Owner):
    job=store.get(jid,owner)
    if job["status"] not in config.TERMINAL: raise HTTPException(409,"Job has not finished")
    p=store.path(jid)/"output"/"report.json"
    if not p.exists(): raise HTTPException(409,"Report is unavailable")
    return json.loads(p.read_text())

@app.get("/api/jobs/{jid}/download/{artifact}")
def download(jid:str,artifact:str,owner:Owner):
    job=store.get(jid,owner)
    if job["status"] not in config.TERMINAL: raise HTTPException(409,"Job has not finished")
    allowed={"report.json"}
    if job["status"] in {"completed","review_required"}: allowed|={"sanitised.txt","sanitised"+job["suffix"]}
    p=store.path(jid)/"output"/artifact
    if artifact not in allowed or not p.is_file(): raise HTTPException(404,"Artifact unavailable")
    media="application/pdf" if artifact.endswith(".pdf") else "text/plain; charset=utf-8" if artifact.endswith(".txt") else None
    return FileResponse(p,filename=artifact,media_type=media,content_disposition_type="inline" if artifact.endswith((".pdf",".txt")) else "attachment")

@app.get("/api/jobs/{jid}/preview/{page}")
def preview(jid:str,page:int,owner:Owner):
    job=store.get(jid,owner)
    if job['status'] not in {'completed','review_required'} or job['suffix']!='.pdf': raise HTTPException(409,'PDF preview unavailable')
    import pymupdf
    with pymupdf.open(store.path(jid)/'output'/'sanitised.pdf') as doc:
        if not 1<=page<=len(doc): raise HTTPException(404,'Page unavailable')
        pix=doc[page-1].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5),alpha=False)
        return Response(pix.tobytes('png'),media_type='image/png',headers={'X-Page-Count':str(len(doc))})

@app.get("/api/jobs/{jid}/original-text")
def original_text(jid:str,owner:Owner):
    job=store.get(jid,owner)
    p=store.path(jid)/"original"/"extracted.json"
    if job["status"] not in {"completed","review_required"} or not p.exists(): raise HTTPException(409,"Original text unavailable")
    return {"warning":"ORIGINAL SENSITIVE CONTENT - never share this preview","segments":json.loads(p.read_text())}

@app.delete("/api/jobs/{jid}",status_code=204)
def delete(jid:str,owner:Owner):
    try: store.delete(jid,owner_id=owner)
    except ValueError as e: raise HTTPException(409,str(e))

class BatchRequest(BaseModel):
    count:int=Field(ge=1,le=50,strict=True)

class RevisionRequest(BaseModel):
    mode:Literal['manual','rerun']='rerun'
    redactions:list[Annotated[str,Field(min_length=1,max_length=512,strict=True)]]=Field(default_factory=list,max_length=100)

@app.post('/api/jobs/{jid}/reprocess',status_code=202)
def reprocess(jid:str,body:RevisionRequest,owner:Owner):
    try:return store.reprocess(jid,body.mode,body.redactions,owner)
    except OverflowError:raise HTTPException(429,'Queue capacity reached; retry shortly',headers={'Retry-After':'2'}) from None
    except (OSError,json.JSONDecodeError):raise HTTPException(409,'Retained source data is unavailable; upload the original again') from None
    except ValueError as error:raise HTTPException(409,str(error)) from None

@app.get('/api/config')
def public_config():
    return {'max_batch_documents':config.MAX_BATCH,'max_file_bytes':config.MAX_BYTES,'retention_hours':config.RETENTION/3600,'supported_formats':sorted(config.SUPPORTED),'workspace':'anonymous-private','authentication':False,'document_isolation':'browser-workspace','https_required':True,'processing':'server-configured detection; local document editing and OCR','detector':detector_config(),'provider_readiness':public_status()}

@app.post('/api/batches',status_code=201)
def create_batch(body:BatchRequest,owner:Owner):
    bid=store.reserve_batch(body.count,owner)
    return {'id':bid,'total':body.count,'status':'awaiting_uploads'}

@app.get('/api/batches/{bid}')
def batch_status(bid:str,owner:Owner):return store.get_batch(bid,owner)

@app.get('/api/batches/{bid}/download')
def batch_download(bid:str,owner:Owner,partial:bool=False):
    batch=store.get_batch(bid,owner)
    if not batch['export_ready'] and not partial:raise HTTPException(409,'Batch is not finished; request a partial ZIP explicitly or wait')
    ready=[item for item in batch['items'] if item['status'] in config.TERMINAL]
    if not ready:raise HTTPException(409,'No finished results are available')
    members=[];total=0
    for item in ready:
        names=['report.json']
        if item['status'] in {'completed','review_required'}:names+=['sanitised'+item['suffix'],'sanitised.txt']
        for name in names:
            path=store.path(item['id'])/'output'/name
            if not path.is_file():raise HTTPException(409,'A result was deleted or expired; refresh the batch')
            total+=path.stat().st_size
            if total>config.MAX_BUNDLE_BYTES:raise HTTPException(413,'Batch ZIP exceeds the configured size limit; use individual downloads')
            members.append((path,f'{item["index"]+1:03d}-{item["id"]}/{name}'))
    # Temporary archive lives on the deployment data volume, not in RAM or the
    # container's small /tmp. It is unlinked immediately and closed after streaming.
    folder=config.DATA/'downloads';folder.mkdir(exist_ok=True,mode=0o700)
    archive=tempfile.TemporaryFile(dir=folder)
    try:
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
            z.writestr('batch-report.json',json.dumps({'batch':bid,'total':batch['total'],'counts':batch['counts'],'complete':batch['export_ready'],'items':batch['items'],'notice':'Sanitised outputs and reports only. A completed pipeline does not guarantee zero PII. Missing/deleted results are identified in the summary.'},indent=2))
            for path,name in members:z.write(path,name)
        archive.seek(0)
    except OSError:
        archive.close();raise HTTPException(409,'Results changed or archive storage is unavailable; refresh and retry') from None
    except BaseException:
        archive.close();raise
    def chunks():
        try:
            while data:=archive.read(1024*1024):yield data
        finally:archive.close()
    return StreamingResponse(chunks(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="veil-batch-{bid}.zip"'})

app.mount("/",StaticFiles(directory=Path(__file__).parent/"static",html=True),name="veil-ui")

def main():
    import uvicorn
    # Access logs include no submitted filename, request body or extracted text.
    uvicorn.run("veil.api:app",host=os.getenv("VEIL_HOST","127.0.0.1"),port=int(os.getenv("VEIL_PORT","8787")),access_log=False,proxy_headers=False,ssl_certfile=os.getenv('VEIL_TLS_CERT_FILE'),ssl_keyfile=os.getenv('VEIL_TLS_KEY_FILE'))
if __name__=="__main__": main()
