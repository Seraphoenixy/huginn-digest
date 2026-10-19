import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from interaction.web import create_app
from orchestration.service import DailyService

class SourceEditTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.settings=Settings(data_dir=Path(self.directory.name),admin_hash=hash_password('abcdefghij'),session_secret='test-edit-source-secret-more-than-32-characters',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.id=self.service.add_source('bili','123','old')
        self.service.store.execute('UPDATE sources SET last_poll=100,error=?,enabled=0 WHERE id=?',('old-error',self.id))

    def tearDown(self):self.directory.cleanup()

    def test_name_only_preserves_progress_and_target_change_resets(self):
        before=self.service.get_source(self.id)
        self.service.edit_source(self.id,'bili','https://space.bilibili.com/123','new name')
        renamed=self.service.get_source(self.id)
        self.assertEqual(renamed['name'],'new name')
        for key in ('created','last_poll','error','enabled'):self.assertEqual(renamed[key],before[key])
        self.service.edit_source(self.id,'bili','456','another')
        updated=self.service.get_source(self.id)
        self.assertEqual(updated['id'],self.id)
        self.assertEqual(updated['target'],'456')
        self.assertEqual(updated['enabled'],0)
        self.assertEqual(updated['last_poll'],0)
        self.assertEqual(updated['error'],'')

    def test_duplicate_invalid_and_type_change(self):
        self.service.add_source('bili','456','second')
        with self.assertRaises(sqlite3.IntegrityError):self.service.edit_source(self.id,'bili','456','duplicate')
        self.assertEqual(self.service.get_source(self.id)['target'],'123')
        with self.assertRaises(ValueError):self.service.edit_source(self.id,'unknown','123','bad')
        with patch('orchestration.service.public_url') as validate:
            self.service.edit_source(self.id,'rss','https://example.com/feed','RSS')
        validate.assert_called_once_with('https://example.com/feed')
        self.assertEqual(self.service.get_source(self.id)['kind'],'rss')

    def test_routes_require_auth_csrf_and_save(self):
        with TestClient(create_app(self.settings, run_background=False)) as client:
            path=f'/admin/sources/{self.id}/edit'
            self.assertEqual(client.get(path).status_code,401)
            page=client.get('/admin/login')
            csrf=re.search('name="token" value="([^"]+)"',page.text)[1]
            client.post('/admin/login',data={'password':'abcdefghij','token':csrf})
            page=client.get(path)
            self.assertIn('编辑订阅来源',page.text)
            self.assertIn('value="123"',page.text)
            csrf=re.search('name="token" value="([^"]+)"',page.text)[1]
            self.assertEqual(client.post(path,data={'kind':'bili','target':'456','name':'new','token':'bad'}).status_code,403)
            response=client.post(path,data={'kind':'bili','target':'456','name':'new','token':csrf})
            self.assertEqual(response.status_code,200)
            self.assertIn('编辑',response.text)
            self.assertEqual(self.service.get_source(self.id)['target'],'456')
            self.assertEqual(client.get('/admin/sources/99999/edit').status_code,404)
