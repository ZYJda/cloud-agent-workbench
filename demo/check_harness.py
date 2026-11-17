"""Small real CLI check for the layered runtime (uses model credits)."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

c=httpx.Client(base_url='http://127.0.0.1:8000',timeout=30)
def call(method,path,**kw):
    response=c.request(method,'/api'+path,**kw)
    response.raise_for_status()
    return response.json()

sid=call('POST','/sessions',json={'title':'架构改造 · 真实续聊验证'})['id']
base='/sessions/'+sid
call('POST',base+'/upload',files={'file':('numbers.txt',b'6\n7\n')})
def run(prompt):
    call('POST',base+'/runs',json={'prompt':prompt})
    for _ in range(300):
        detail=call('GET',base)
        if detail['runs'][-1]['status'] not in ('queued','running'):
            assert detail['runs'][-1]['status']=='succeeded',detail['runs'][-1]
            return detail
        time.sleep(2)
    raise TimeoutError('CLI verification timed out')

first=run('只做一个极小验证任务：读取上传的 numbers.txt，将两行整数之和写入 artifacts/result.txt，内容只需 13。用一条 shell 命令完成读取、计算、写入和打印结果即可。不要编写额外脚本、测试或说明文件。最后简短确认。')
thread=first['session']['thread_id']
second=run('继续上轮工作：保留 artifacts/result.txt 中的数字，在文件末尾追加一行 VERIFIED。只修改这一个文件，读取确认即可，不要创建其他文件。')
assert second['session']['thread_id']==thread
r=c.get('/api'+base+'/file',params={'path':'artifacts/result.txt'});r.raise_for_status()
assert b'13' in r.content and b'VERIFIED' in r.content
manifests=[m for m in second['artifacts'] if m['path']=='artifacts/result.txt']
assert len(manifests)==2
assert manifests[0]['sha256']!=manifests[1]['sha256']
assert manifests[-1]['sha256']==hashlib.sha256(r.content).hexdigest()
out=Path(__file__).resolve().parents[1]/'evidence'/'harness-refactor.json'
out.write_text(json.dumps({'verified_at':datetime.now(timezone.utc).isoformat(),'health':call('GET','/health'),'verified':['real CLI','uploaded context','resume same thread','two per-run artifact hashes','download hash matches'],'downloaded_result':r.content.decode('utf-8'),'session':second},ensure_ascii=False,indent=2),encoding='utf-8')
print('PASS: real CLI, upload context, resume, artifact manifests and download integrity',flush=True)
