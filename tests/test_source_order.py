import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.store import Store
from interaction.web import create_app


class SourceOrderTests(unittest.TestCase):
    def test_migration_and_atomic_order_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE sources(id INTEGER PRIMARY KEY,kind TEXT,target TEXT,name TEXT,enabled INTEGER DEFAULT 1,created REAL,last_poll REAL DEFAULT 0,error TEXT DEFAULT "",UNIQUE(kind,target))')
                db.executemany('INSERT INTO sources(kind,target,name,created) VALUES("bili",?,?,1)',[('1','one'),('2','two')])
            db.close()
            store=Store(path)
            self.assertEqual([r['id'] for r in store.sources()],[2,1])
            store.reorder_sources([1,2])
            self.assertEqual([r['id'] for r in Store(path).sources()],[1,2])
            for invalid in ([1,1],[1],[1,99]):
                with self.assertRaises(ValueError):store.reorder_sources(invalid)
            self.assertEqual([r['id'] for r in store.sources()],[1,2])
            third=store.add_source('bili','3','three')
            self.assertEqual([r['id'] for r in store.sources()],[1,2,third])

    def test_route_auth_csrf_conflict_and_script(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(data_dir=Path(directory),admin_hash=hash_password('abcdefghij'),session_secret='order-test-secret-more-than-32-characters',secure_cookie=False)
            app=create_app(settings, run_background=False);service=app.state.service
            first=service.add_source('bili','1','one');second=service.add_source('bili','2','two')
            with TestClient(app) as client:
                payload={'order':f'{second},{first}','token':'bad'}
                self.assertEqual(client.post('/admin/sources/order',data=payload).status_code,401)
                page=client.get('/admin/login');token=re.search('name="token" value="([^"]+)"',page.text)[1]
                page=client.post('/admin/login',data={'password':'abcdefghij','token':token})
                token=re.search('name="token" value="([^"]+)"',page.text)[1]
                self.assertIn('source-grip',page.text)
                self.assertIn('source-kind">B站</span>',page.text)
                self.assertNotIn('<small>bili</small>',page.text)
                self.assertEqual(client.post('/admin/sources/order',data=payload).status_code,403)
                payload['token']=token
                self.assertEqual(client.post('/admin/sources/order',data=payload).status_code,200)
                self.assertEqual([r['id'] for r in service.store.sources()],[second,first])
                service.add_source('bili','3','three')
                self.assertEqual(client.post('/admin/sources/order',data=payload).status_code,409)
                script=client.get('/static/source-order.js')
                self.assertEqual(script.status_code,200)
                self.assertIn("script-src 'self'",script.headers['content-security-policy'])
