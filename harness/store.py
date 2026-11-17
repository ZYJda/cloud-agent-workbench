import contextlib
import sqlite3
import json
import os
import time
from .errors import DomainError

def redact(value):
    text = str(value)
    key = os.getenv('AGENT_API_KEY', '')
    return text.replace(key, '[REDACTED]') if key else text

class SQLiteStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS artifacts(session_id TEXT,run_id TEXT,path TEXT,size INTEGER,sha256 TEXT,created REAL,PRIMARY KEY(run_id,path));
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,created REAL,thread_id TEXT);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,prompt TEXT,status TEXT,created REAL,finished REAL,error TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,run_id TEXT,kind TEXT,payload TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE UNIQUE INDEX IF NOT EXISTS active_run ON runs(session_id) WHERE status IN ('queued','running');
            ''')

    def query(self, sql, args=(), one=False):
        with contextlib.closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.row_factory = sqlite3.Row
            cur = db.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows

    def event(self, sid, rid, kind, payload):
        self.query('INSERT INTO events(session_id,run_id,kind,payload,created) VALUES(?,?,?,?,?)',
              (sid, rid, kind, redact(json.dumps(payload, ensure_ascii=False)), time.time()))

    def session(self, sid):
        row = self.query('SELECT * FROM sessions WHERE id=?', (sid,), True)
        if not row:
            raise DomainError(404, '会话不存在')
        return row

    def recover(self):
        interrupted = self.query("SELECT id,session_id FROM runs WHERE status IN ('running','queued')")
        self.query("UPDATE runs SET status='interrupted',error='服务已重启，请重新发送以继续。',finished=? WHERE status IN ('running','queued')", (time.time(),))
        for run in interrupted:
            self.event(run['session_id'],run['id'],'status',{'status':'interrupted','error':'服务已重启，请重新发送以继续。'})

    def record_artifacts(self, sid, rid, entries):
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM artifacts WHERE run_id=?',(rid,))
            db.executemany('INSERT INTO artifacts VALUES(?,?,?,?,?,?)',
                           [(sid,rid,e['path'],e['size'],e['sha256'],time.time()) for e in entries])
