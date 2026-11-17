import os
import hashlib
from pathlib import Path
from .errors import DomainError

class WorkspaceService:
    def __init__(self, store, data):
        self.store, self.data = store, data

    def workspace(self, sid):
        self.store.session(sid)
        p = self.data / 'sessions' / sid / 'workspace'
        p.mkdir(parents=True, exist_ok=True)
        return p

    def safe_file(self, sid, name):
        base = self.workspace(sid).resolve()
        candidate = base / name
        if any(x.startswith('.') for x in Path(name).parts) or '\\' in name:
            raise DomainError(400, '不允许的文件路径')
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base) or resolved == base:
            raise DomainError(400, '文件必须位于当前工作区')
        # Refuse symlinks even if their targets are currently within this workspace.
        if any(p.is_symlink() for p in [candidate, *candidate.parents] if p != base.parent):
            raise DomainError(400, '不允许符号链接')
        return resolved

    def files(self, sid):
        base = self.workspace(sid)
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

    def artifacts(self, sid):
        entries=[]
        for entry in self.files(sid):
            if not entry['path'].startswith('artifacts/'):
                continue
            digest=hashlib.sha256()
            with self.safe_file(sid,entry['path']).open('rb') as f:
                for chunk in iter(lambda:f.read(65536),b''):
                    digest.update(chunk)
            entries.append({**entry,'sha256':digest.hexdigest()})
        return entries
