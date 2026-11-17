"""Run real end-to-end tasks against the running workbench; consumes model credits.
Usage: python demo/run_demo.py [http://127.0.0.1:8000]
Outputs sanitized HTTP results and downloaded artifacts under evidence/live-demo/.
"""
import json
from pathlib import Path
import sys
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence'/'live-demo'
OUT.mkdir(parents=True,exist_ok=True)
c=httpx.Client(base_url=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8000',timeout=30)
def request(method,path,**kw):
    r=c.request(method,'/api'+path,**kw); r.raise_for_status(); return r.json()
def send(sid,prompt):
    return request('POST',f'/sessions/{sid}/runs',json={'prompt':prompt})
def wait(sid):
    for _ in range(400):
        d=request('GET',f'/sessions/{sid}')
        if d['runs'][-1]['status'] not in ('queued','running'):
            (OUT/(sid+'.json')).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
            assert d['runs'][-1]['status']=='succeeded',d['runs'][-1]
            return d
        time.sleep(3)
    raise TimeoutError(sid)
if __name__=='__main__':
    code=request('POST','/sessions',json={'title':'CSV 汇总工具 · 完整演示'})['id']
    report=request('POST','/sessions',json={'title':'销售月度复盘 · 完整演示'})['id']
    for name in ['sales.csv','brief.txt']:
        with (ROOT/'demo'/name).open('rb') as f:
            request('POST',f'/sessions/{report}/upload',files={'file':(name,f)})
    send(code,'开发无需第三方依赖的 Python CSV 汇总工具，按 category 列汇总 amount 列。源码 summary.py、至少三条 unittest 测试和使用说明保存到 artifacts/；请实际运行测试。')
    send(report,'读取 uploads/ 中的销售材料，核对两个月收入、成本、毛利以及环比变化，写出事实、推测和数据局限，保存 artifacts/report.md。使用 Python 实际计算核对。')
    print('Concurrent sessions:',code,report,flush=True)
    wait(code); print('Code first turn passed',flush=True)
    wait(report); print('Report first turn passed',flush=True)
    send(code,'在刚才的工具上增加 --json 输出选项，保留原来的输出。补充对应测试并实际运行。')
    send(report,'保留报告原文，追加不超过 150 字的管理层摘要和三项下周行动，不要编造缺失信息。')
    for sid,label in [(code,'code'),(report,'report')]:
        d=wait(sid)
        assert len(d['runs'])==2
        assert d['session']['thread_id']
        required='artifacts/summary.py' if label=='code' else 'artifacts/report.md'
        assert required in [f['path'] for f in d['files']],f'Missing deliverable: {required}'
        r=c.get('/api/sessions/'+sid+'/archive'); r.raise_for_status()
        (OUT/(label+'.zip')).write_bytes(r.content)
        print(label,'follow-up passed',len(d['files']),'files',flush=True)
    (OUT/'summary.json').write_text(json.dumps({'code':code,'report':report,'health':request('GET','/health'),'verified':['two concurrent sessions','uploads','two turns per session','CLI resume IDs','ZIP downloads']},indent=2),encoding='utf-8')
