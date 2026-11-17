import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import zipfile

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from harness.config import Settings
from harness.store import SQLiteStore, redact
from harness.workspace import WorkspaceService
from harness.context import ContextPipeline, ThreadContextMiddleware, UploadsMiddleware
from harness.driver import CodexDriver
from harness.execution import LocalProcessProvider
from harness.runtime import RunManager
from harness.events import EventStream
from harness.errors import DomainError

ROOT = Path(__file__).parent.resolve()
load_dotenv(ROOT / '.env')
settings = Settings.from_env(ROOT)
store = SQLiteStore(settings.data_dir/'workbench.sqlite3')
workspaces = WorkspaceService(store, settings.data_dir)
pipeline = ContextPipeline([ThreadContextMiddleware(workspaces), UploadsMiddleware(workspaces)])
manager = RunManager(store,workspaces,pipeline,CodexDriver(),LocalProcessProvider(),settings)
events = EventStream(store)
store.recover()
# Backward-compatible Python entry points; HTTP and stored data contracts are unchanged.
query,event,session = store.query,store.event,store.session
workspace,safe_file,files = workspaces.workspace,workspaces.safe_file,workspaces.files
pool,execute = manager.pool,manager.execute

@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    manager.shutdown()

app = FastAPI(title='云端 Agent 工作台', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost','127.0.0.1','[::1]','testserver'])

@app.exception_handler(DomainError)
async def domain_error(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':exc.detail},status_code=exc.status_code)

@app.middleware('http')
async def guard(request, call_next):
    # Reject browser cross-origin writes; localhost deployment has no login by default.
    origin = request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and origin != str(request.base_url).rstrip('/'):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'跨域请求已拒绝'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'none'; object-src 'none'; frame-ancestors 'none'"
    return response

class NewSession(BaseModel):
    title: str = Field(default='新的工作', min_length=1, max_length=80)

class Message(BaseModel):
    prompt: str = Field(min_length=1,max_length=30000)

@app.get('/api/health')
def health():
    import shutil
    return {'ok':True,'configured':bool(os.getenv('AGENT_API_KEY') and os.getenv('AGENT_MODEL')),
            'cli':bool(shutil.which(os.getenv('CODEX_BIN','codex'))),'model':os.getenv('AGENT_MODEL','未配置')}

@app.get('/api/sessions')
def list_sessions():
    return query('SELECT s.*, (SELECT status FROM runs WHERE session_id=s.id ORDER BY created DESC LIMIT 1) AS status FROM sessions s ORDER BY created DESC')

@app.post('/api/sessions',status_code=201)
def create_session(body:NewSession):
    sid = str(uuid.uuid4())
    query('INSERT INTO sessions VALUES(?,?,?,NULL)',(sid,body.title.strip() or '新的工作',time.time()))
    workspace(sid)
    return session(sid)

@app.patch('/api/sessions/{sid}')
def rename(sid:str, body:NewSession):
    session(sid)
    query('UPDATE sessions SET title=? WHERE id=?',(body.title,sid))
    return session(sid)

@app.get('/api/sessions/{sid}')
def detail(sid:str):
    return {'session':session(sid),'runs':query('SELECT * FROM runs WHERE session_id=? ORDER BY created',(sid,)), 'files':files(sid), 'artifacts':query('SELECT * FROM artifacts WHERE session_id=? ORDER BY created',(sid,))}

@app.post('/api/sessions/{sid}/runs',status_code=202)
def send(sid:str, body:Message):
    return manager.submit(sid,body.prompt)

@app.post('/api/sessions/{sid}/stop')
def stop(sid:str):
    manager.cancel(sid)
    return {'ok':True}

@app.get('/api/sessions/{sid}/events')
async def stream(sid:str, request:Request, after:int=0):
    session(sid)
    cursor = max(after,int(request.headers.get('last-event-id','0') or '0'))
    return StreamingResponse(events.iterate(sid,cursor,request),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.post('/api/sessions/{sid}/upload')
async def upload(sid:str, file:UploadFile):
    session(sid)
    name = file.filename or 'file'
    if Path(name).name != name or '\\' in name or name.startswith('.'):
        raise HTTPException(400,'非法文件名')
    path = safe_file(sid,'uploads/'+uuid.uuid4().hex[:8]+'-'+name)
    path.parent.mkdir(parents=True,exist_ok=True)
    staging = path.with_name('.upload-'+uuid.uuid4().hex+'.part')
    total = 0
    try:
        with staging.open('xb') as output:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > 20*1024*1024:
                    raise HTTPException(413,'单个文件最多 20 MB')
                output.write(chunk)
        staging.replace(path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    relative = path.relative_to(workspace(sid)).as_posix()
    event(sid,None,'upload',{'path':relative,'size':total})
    return {'path':relative}

@app.get('/api/sessions/{sid}/file')
def download(sid:str,path:str):
    target = safe_file(sid,path)
    if not target.is_file():
        raise HTTPException(404,'文件不存在')
    return FileResponse(target,filename=target.name,media_type='application/octet-stream')

@app.get('/api/sessions/{sid}/preview')
def preview(sid:str,path:str):
    target = safe_file(sid,path)
    if not target.is_file():
        raise HTTPException(404,'文件不存在')
    if target.stat().st_size > 200000:
        raise HTTPException(413,'文件较大，请下载查看')
    try:
        return {'text':target.read_text(encoding='utf-8')}
    except UnicodeError:
        raise HTTPException(415,'二进制文件请下载查看')

@app.get('/api/sessions/{sid}/archive')
def archive(sid:str):
    entries = files(sid)
    if sum(f['size'] for f in entries) > 100*1024*1024:
        raise HTTPException(413,'工作区超过 100 MB，请分别下载')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for f in entries:
            z.write(safe_file(sid,f['path']),f['path'])
    return StreamingResponse(iter([buf.getvalue()]),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="workspace.zip"'})

app.mount('/',StaticFiles(directory=ROOT/'static',html=True),name='ui')
