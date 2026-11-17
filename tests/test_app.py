"""Offline regression tests. CLI responses are mocked; these are not model evidence."""
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

TEMP = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = TEMP.name
os.environ['AGENT_API_KEY'] = 'unit-test-secret'
os.environ['AGENT_MODEL'] = 'test-model'
import app
from fastapi.testclient import TestClient

class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.submit = patch.object(app.pool,'submit').start()
        self.sid = self.client.post('/api/sessions',json={'title':'test'}).json()['id']
        self.base = '/api/sessions/'+self.sid

    def tearDown(self):
        patch.stopall()

    def send(self):
        return self.client.post(self.base+'/runs',json={'prompt':'do work'})

    def test_sessions_are_isolated(self):
        other = self.client.post('/api/sessions',json={'title':'other'}).json()['id']
        self.client.post(self.base+'/upload',files={'file':('a.txt',b'hello')})
        self.assertEqual(len(self.client.get(self.base).json()['files']),1)
        self.assertEqual(self.client.get('/api/sessions/'+other).json()['files'],[])

    def test_duplicate_run_conflicts(self):
        self.assertEqual(self.send().status_code,202)
        self.assertEqual(self.send().status_code,409)
        self.assertEqual(self.submit.call_count,1)

    def test_stop_queued_and_continue(self):
        rid = self.send().json()['id']
        self.client.post(self.base+'/stop')
        app.execute(self.sid,rid)
        self.assertEqual(self.client.get(self.base).json()['runs'][0]['status'],'cancelled')
        self.assertEqual(self.send().status_code,202)

    def test_upload_duplicates_preserved(self):
        first = self.client.post(self.base+'/upload',files={'file':('a.txt',b'one')}).json()['path']
        second = self.client.post(self.base+'/upload',files={'file':('a.txt',b'two')}).json()['path']
        self.assertNotEqual(first,second)
        self.assertEqual(self.client.get(self.base+'/file',params={'path':first}).content,b'one')

    def test_path_traversal_and_hidden_files_blocked(self):
        for name in ['../../workbench.sqlite3','/etc/passwd','.env','uploads/../.env','a\\b']:
            self.assertEqual(self.client.get(self.base+'/file',params={'path':name}).status_code,400)
        self.assertEqual(self.client.post(self.base+'/upload',files={'file':('../x.txt',b'bad')}).status_code,400)

    def test_html_preview_is_json_and_download_is_attachment(self):
        path = self.client.post(self.base+'/upload',files={'file':('x.html',b'<script>alert(1)</script>')}).json()['path']
        response = self.client.get(self.base+'/preview',params={'path':path})
        self.assertEqual(response.json()['text'],'<script>alert(1)</script>')
        self.assertIn('application/json',response.headers['content-type'])
        self.assertIn('attachment',self.client.get(self.base+'/file',params={'path':path}).headers['content-disposition'])

    def test_cross_origin_mutation_rejected(self):
        self.assertEqual(self.client.post('/api/sessions',json={'title':'x'},headers={'Origin':'https://evil.example'}).status_code,403)

    def test_worker_events_and_resume_id(self):
        rid = self.send().json()['id']
        proc = MagicMock()
        proc.stdout = io.BytesIO(b'{"type":"thread.started","thread_id":"thread-123"}\n{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n{"type":"turn.completed"}\n')
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with patch.object(app.subprocess,'Popen',return_value=proc) as spawn:
            app.execute(self.sid,rid)
            self.assertNotIn('unit-test-secret',str(spawn.call_args.args))
        self.assertEqual(app.session(self.sid)['thread_id'],'thread-123')
        self.assertEqual(self.client.get(self.base).json()['runs'][0]['status'],'succeeded')
        self.assertTrue(app.query('SELECT * FROM events WHERE session_id=?',(self.sid,)))

    def test_cli_failure_is_terminal_and_secret_redacted(self):
        rid = self.send().json()['id']
        with patch.object(app.subprocess,'Popen',side_effect=OSError('unit-test-secret failed')):
            app.execute(self.sid,rid)
        run=self.client.get(self.base).json()['runs'][0]
        self.assertEqual(run['status'],'failed')
        self.assertNotIn('unit-test-secret',run['error'])

    def test_blank_prompt_rejected(self):
        self.assertEqual(self.client.post(self.base+'/runs',json={'prompt':'  '}).status_code,400)

    def test_restart_preserves_history_and_marks_interrupted(self):
        self.send()
        subprocess.run([sys.executable,'-c','import app'],check=True,env=os.environ.copy())
        run=self.client.get(self.base).json()['runs'][0]
        self.assertEqual(run['status'],'interrupted')
        self.assertEqual(run['prompt'],'do work')

    def test_host_rebinding_blocked(self):
        self.assertEqual(self.client.get('/api/sessions',headers={'host':'evil.example'}).status_code,400)

    def test_cli_exit_zero_without_completed_event_is_failure(self):
        rid = self.send().json()['id']
        proc=MagicMock()
        proc.stdout=io.BytesIO(b'')
        proc.wait.return_value=0
        proc.poll.return_value=0
        with patch.object(app.subprocess,'Popen',return_value=proc):
            app.execute(self.sid,rid)
        self.assertEqual(self.client.get(self.base).json()['runs'][0]['status'],'failed')

if __name__ == '__main__':
    unittest.main()
