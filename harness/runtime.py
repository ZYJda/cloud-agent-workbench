import concurrent.futures
import threading
import json
import time
import uuid
from .store import redact
from .errors import DomainError
from .context import RunContext

class RunManager:
    def __init__(self, store, workspaces, pipeline, driver, provider, settings):
        self.store,self.workspaces,self.pipeline = store,workspaces,pipeline
        self.driver,self.provider,self.settings = driver,provider,settings
        self.lock=threading.RLock()
        self.processes={}
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=settings.workers)

    def submit(self, sid, prompt):
        self.store.session(sid)
        if not prompt.strip(): raise DomainError(400,'请输入任务')
        rid=str(uuid.uuid4())
        with self.lock:
            if self.store.query("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running')",(sid,),True):
                raise DomainError(409,'当前会话仍在执行，请等待或停止后继续')
            self.store.query('INSERT INTO runs VALUES(?,?,?,?,?,NULL,NULL)',(rid,sid,prompt,'queued',time.time()))
            self.store.event(sid,rid,'user',{'text':prompt})
            self.pool.submit(self.execute,sid,rid)
        return {'id':rid,'status':'queued'}

    def cancel(self, sid):
        self.store.session(sid)
        with self.lock:
            row=self.store.query("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running')",(sid,),True)
            if row:
                self.store.query("UPDATE runs SET status='cancelled',finished=? WHERE id=?",(time.time(),row['id']))
                if row['id'] in self.processes: self.provider.terminate(self.processes[row['id']])
                self.store.event(sid,row['id'],'status',{'status':'cancelled'})

    def shutdown(self):
        with self.lock:
            for proc in list(self.processes.values()): self.provider.terminate(proc)
        self.pool.shutdown(wait=False,cancel_futures=True)

    def execute(self, sid, rid):
        proc = None
        timer = None
        timed_out = threading.Event()
        try:
            with self.lock:
                run = self.store.query('SELECT * FROM runs WHERE id=?', (rid,), True)
                if run['status'] != 'queued':
                    return
                self.store.query("UPDATE runs SET status='running' WHERE id=?", (rid,))
            self.store.event(sid, rid, 'status', {'status':'running'})
            ctx = self.pipeline.prepare(RunContext(sid,rid,run['prompt']))
            spec = self.driver.prepare(ctx,self.store.session(sid)['thread_id'])
            with self.lock:
                if self.store.query('SELECT status FROM runs WHERE id=?',(rid,),True)['status'] == 'cancelled':
                    return
                proc = self.provider.start(spec)
                self.processes[rid] = proc
            def timeout():
                timed_out.set()
                self.provider.terminate(proc)
            timer = threading.Timer(self.settings.timeout, timeout)
            timer.start()
            proc.stdin.write(spec.prompt.encode('utf-8'))
            proc.stdin.close()
            failed = False
            completed = False
            last_error = ''
            for raw in iter(proc.stdout.readline, b''):
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    self.store.event(sid,rid,'log',{'text':line[:12000]})
                    continue
                if item.get('type') == 'thread.started':
                    self.store.query('UPDATE sessions SET thread_id=? WHERE id=?', (item['thread_id'],sid))
                if item.get('type') == 'turn.completed':
                    completed = True
                if item.get('type') == 'turn.failed':
                    failed = True
                    last_error = str(item.get('error',{}).get('message',''))
                elif item.get('type') == 'error':
                    last_error = str(item.get('message',''))
                self.store.event(sid,rid,'agent',item)
            rc = proc.wait()
            if timed_out.is_set():
                raise RuntimeError('任务超时已停止；已有成果已保留，可缩小任务后继续。')
            if rc != 0 or failed or not completed:
                explanation = '请求过于频繁，请等待其他任务完成后重试。' if '429' in last_error else '请查看执行记录，检查模型、额度或网络后重试。'
                raise RuntimeError(f'Agent 执行失败（退出码 {rc}）。{explanation} {last_error[:1000]}')
            entries = self.workspaces.artifacts(sid)
            self.store.record_artifacts(sid,rid,entries)
            self.store.event(sid,rid,'artifacts',{'files':entries})
            with self.lock:
                self.store.query("UPDATE runs SET status='succeeded',finished=? WHERE id=? AND status='running'", (time.time(),rid))
        except Exception as exc:
            self.store.query("UPDATE runs SET status='failed',error=?,finished=? WHERE id=? AND status IN ('queued','running')",(redact(exc),time.time(),rid))
        finally:
            if timer:
                timer.cancel()
            if proc:
                self.provider.terminate(proc)
            with self.lock:
                self.processes.pop(rid,None)
            row = self.store.query('SELECT status,error FROM runs WHERE id=?',(rid,),True)
            self.store.event(sid,rid,'status',row)

