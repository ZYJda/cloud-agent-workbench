import asyncio
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent.resolve()
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('DATA_DIR', ROOT / 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'workbench.sqlite3'
pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
lock = threading.RLock()
processes = {}
TERMINAL = ('succeeded', 'failed', 'cancelled', 'interrupted')

def query(sql, args=(), one=False):
    with contextlib.closing(sqlite3.connect(DB, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        cur = db.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        return (rows[0] if rows else None) if one else rows

with contextlib.closing(sqlite3.connect(DB)) as db, db:
    db.executescript('''PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,created REAL,thread_id TEXT);
    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,prompt TEXT,status TEXT,created REAL,finished REAL,error TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,run_id TEXT,kind TEXT,payload TEXT,created REAL);
    CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
    CREATE UNIQUE INDEX IF NOT EXISTS active_run ON runs(session_id) WHERE status IN ('queued','running');
    ''')
query("UPDATE runs SET status='interrupted',error='服务已重启，请重新发送以继续。',finished=? WHERE status IN ('running','queued')", (time.time(),))

def redact(value):
    text = str(value)
    key = os.getenv('AGENT_API_KEY', '')
    return text.replace(key, '[REDACTED]') if key else text

def event(sid, rid, kind, payload):
    query('INSERT INTO events(session_id,run_id,kind,payload,created) VALUES(?,?,?,?,?)',
          (sid, rid, kind, redact(json.dumps(payload, ensure_ascii=False)), time.time()))

def session(sid):
    row = query('SELECT * FROM sessions WHERE id=?', (sid,), True)
    if not row:
        raise HTTPException(404, '会话不存在')
    return row

def workspace(sid):
    session(sid)
    p = DATA / 'sessions' / sid / 'workspace'
    p.mkdir(parents=True, exist_ok=True)
    return p

def safe_file(sid, name):
    base = workspace(sid).resolve()
    candidate = base / name
    if any(x.startswith('.') for x in Path(name).parts) or '\\' in name:
        raise HTTPException(400, '不允许的文件路径')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise HTTPException(400, '文件必须位于当前工作区')
    # Refuse symlinks even if their targets are currently within this workspace.
    if any(p.is_symlink() for p in [candidate, *candidate.parents] if p != base.parent):
        raise HTTPException(400, '不允许符号链接')
    return resolved

def files(sid):
    base = workspace(sid)
    found = []
    for directory, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules','__pycache__') and not (Path(directory)/d).is_symlink()]
        for name in names:
            if name.startswith('.'):
                continue
            p = Path(directory)/name
            if p.is_symlink() or not p.is_file():
                continue
            found.append({'path': p.relative_to(base).as_posix(), 'size': p.stat().st_size})
            if len(found) >= 1000:
                return found
    return sorted(found, key=lambda f: f['path'])

def terminate(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

def execute(sid, rid):
    proc = None
    timer = None
    timed_out = threading.Event()
    try:
        with lock:
            run = query('SELECT * FROM runs WHERE id=?', (rid,), True)
            if run['status'] != 'queued':
                return
            query("UPDATE runs SET status='running' WHERE id=?", (rid,))
        event(sid, rid, 'status', {'status':'running'})
        if not os.getenv('AGENT_API_KEY') or not os.getenv('AGENT_MODEL'):
            raise RuntimeError('请在服务端 .env 配置 AGENT_API_KEY 和 AGENT_MODEL 后重启。')
        home = DATA / 'sessions' / sid / 'codex'
        home.mkdir(parents=True, exist_ok=True)
        config = '\n'.join([
            'model_provider = "exam"',
            'model = '+json.dumps(os.environ['AGENT_MODEL']),
            'model_reasoning_effort = "medium"', 'web_search = "disabled"',
            '[model_providers.exam]', 'name = "Exam API"',
            'base_url = '+json.dumps(os.getenv('AGENT_BASE_URL','https://api.openai.com/v1')),
            'env_key = "AGENT_API_KEY"', 'wire_api = "responses"',
            'request_max_retries = 2', 'stream_max_retries = 2',
            '[features]', 'multi_agent = false',
            '[agents]', 'enabled = false',
        ])
        (home/'config.toml').write_text(config, encoding='utf-8')
        env = {k:v for k,v in os.environ.items() if k.upper() in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','COMSPEC','PATHEXT','LANG')}
        env.update(CODEX_HOME=str(home), AGENT_API_KEY=os.environ['AGENT_API_KEY'], NO_COLOR='1')
        args = [os.getenv('CODEX_BIN','codex'), 'exec']
        thread_id = session(sid)['thread_id']
        if thread_id:
            args += ['resume', thread_id]
        args += ['--json','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','-']
        prompt = ('你是云端单用户工作台的助手。仅在当前工作目录处理任务；上传材料在 uploads/，交付成果请写入 artifacts/。'
                  '不要读取环境变量或凭据，不要访问其他会话。请实际完成任务并验证，最后用中文说明结果和成果路径。\n\n'+run['prompt'])
        with lock:
            if query('SELECT status FROM runs WHERE id=?',(rid,),True)['status'] == 'cancelled':
                return
            proc = subprocess.Popen(args, cwd=workspace(sid), env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=os.name != 'nt')
            processes[rid] = proc
        def timeout():
            timed_out.set()
            terminate(proc)
        timer = threading.Timer(int(os.getenv('RUN_TIMEOUT_SECONDS','900')), timeout)
        timer.start()
        proc.stdin.write(prompt.encode('utf-8'))
        proc.stdin.close()
        failed = False
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode('utf-8', errors='replace').strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                event(sid,rid,'log',{'text':line[:12000]})
                continue
            if item.get('type') == 'thread.started':
                query('UPDATE sessions SET thread_id=? WHERE id=?', (item['thread_id'],sid))
            if item.get('type') == 'turn.failed':
                failed = True
            event(sid,rid,'agent',item)
        rc = proc.wait()
        if timed_out.is_set():
            raise RuntimeError('任务超时已停止；已有成果已保留，可缩小任务后继续。')
        if rc != 0 or failed:
            raise RuntimeError(f'Agent 执行失败（退出码 {rc}）。请查看执行记录，检查模型、额度或网络后重试。')
        with lock:
            query("UPDATE runs SET status='succeeded',finished=? WHERE id=? AND status='running'", (time.time(),rid))
    except Exception as exc:
        query("UPDATE runs SET status='failed',error=?,finished=? WHERE id=? AND status IN ('queued','running')",(redact(exc),time.time(),rid))
    finally:
        if timer:
            timer.cancel()
        if proc:
            terminate(proc)
        with lock:
            processes.pop(rid,None)
        row = query('SELECT status,error FROM runs WHERE id=?',(rid,),True)
        event(sid,rid,'status',row)

@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    with lock:
        for proc in list(processes.values()):
            terminate(proc)
    pool.shutdown(wait=False, cancel_futures=True)

app = FastAPI(title='云端 Agent 工作台', lifespan=lifespan)

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
    return {'session':session(sid),'runs':query('SELECT * FROM runs WHERE session_id=? ORDER BY created',(sid,)), 'files':files(sid)}

@app.post('/api/sessions/{sid}/runs',status_code=202)
def send(sid:str, body:Message):
    session(sid)
    if not body.prompt.strip():
        raise HTTPException(400,'请输入任务')
    rid = str(uuid.uuid4())
    with lock:
        if query("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running')",(sid,),True):
            raise HTTPException(409,'当前会话仍在执行，请等待或停止后继续')
        query('INSERT INTO runs VALUES(?,?,?,?,?,NULL,NULL)',(rid,sid,body.prompt,'queued',time.time()))
        event(sid,rid,'user',{'text':body.prompt})
        pool.submit(execute,sid,rid)
    return {'id':rid,'status':'queued'}

@app.post('/api/sessions/{sid}/stop')
def stop(sid:str):
    session(sid)
    with lock:
        row = query("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running')",(sid,),True)
        if row:
            query("UPDATE runs SET status='cancelled',finished=? WHERE id=?",(time.time(),row['id']))
            if row['id'] in processes:
                terminate(processes[row['id']])
            event(sid,row['id'],'status',{'status':'cancelled'})
    return {'ok':True}

@app.get('/api/sessions/{sid}/events')
async def stream(sid:str, request:Request, after:int=0):
    session(sid)
    cursor = max(after,int(request.headers.get('last-event-id','0') or '0'))
    async def generate():
        nonlocal cursor
        while not await request.is_disconnected():
            rows = query('SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 100',(sid,cursor))
            for row in rows:
                cursor = row['id']
                row['payload'] = json.loads(row['payload'])
                yield f'id: {cursor}\ndata: {json.dumps(row,ensure_ascii=False)}\n\n'
            if not rows:
                yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.post('/api/sessions/{sid}/upload')
async def upload(sid:str, file:UploadFile):
    session(sid)
    name = file.filename or 'file'
    if Path(name).name != name or '\\' in name or name.startswith('.'):
        raise HTTPException(400,'非法文件名')
    path = safe_file(sid,'uploads/'+uuid.uuid4().hex[:8]+'-'+name)
    path.parent.mkdir(parents=True,exist_ok=True)
    total = 0
    try:
        with path.open('xb') as output:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > 20*1024*1024:
                    raise HTTPException(413,'单个文件最多 20 MB')
                output.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
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
