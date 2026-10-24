"""模型配置可在管理页保存：只写配置文件的对应键，密钥不回显。"""
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from config import ROOT, Settings
from interaction.web import create_app


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        self.config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        self.settings=Settings(config_file=self.config,session_secret='model-config-secret-at-least-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def token(self):
        return re.search('name="token" value="([^"]+)"',self.client.get('/admin').text)[1]

    def section(self,name):
        return tomllib.loads(self.config.read_text(encoding='utf-8')).get(name,{})

    def post(self,**fields):
        data={'token':self.token(),**fields}
        return self.client.post('/admin/config/models',data=data,follow_redirects=False)

    def test_page_shows_values_as_editable_entries(self):
        page=self.client.get('/admin').text
        self.assertIn('摘要与转写模型',page)
        self.assertIn('/admin/config/models',page)
        # 摘要与转写各占一行，输入框常驻，没有折叠态。
        self.assertEqual(page.count('class="model-row"'),2)
        self.assertNotIn('<details class="model-row"',page)
        for name in ('llm_model','llm_base_url','llm_key','asr_model','asr_base_url','asr_key'):
            self.assertIn(f'name="{name}"',page)
        self.assertIn('type="password"',page)
        # 密码框不带 value；未配置时显示“未配置”而不是空白。
        self.assertNotIn('name="llm_key" value=',page)
        self.assertNotIn('name="asr_key" value=',page)
        # 未配置时直接在 Key 输入框里说明状态，不需要折叠摘要行。
        self.assertEqual(page.count('placeholder="Key 未配置，粘贴后保存"'),2)

    def test_configured_keys_are_shown_as_hidden(self):
        self.post(llm_key='sk-hidden-fixture',asr_key='asr-hidden-fixture')
        page=self.client.get('/admin').text
        self.assertEqual(page.count('placeholder="Key 已隐藏，留空保持不变"'),2)
        self.assertNotIn('sk-hidden-fixture',page)
        self.assertNotIn('asr-hidden-fixture',page)

    def test_saving_model_keeps_existing_key(self):
        response=self.post(llm_model='deepseek-v4.1-flash-pro',llm_base_url='https://ark.example.com/api/v3')
        self.assertEqual(response.status_code,303)
        llm=self.section('llm')
        self.assertEqual(llm['model'],'deepseek-v4.1-flash-pro')
        self.assertEqual(llm['base_url'],'https://ark.example.com/api/v3')
        self.assertEqual(llm['api_key'],'', '留空的 Key 不应被清空或改写')

    def test_saving_key_updates_file_and_hides_it(self):
        secret='sk-model-config-test-value'
        response=self.post(llm_key=secret)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.section('llm')['api_key'],secret)
        self.assertTrue(self.settings.llm_state()['configured'])
        self.assertNotIn(secret,self.client.get('/admin').text)

    def test_invalid_base_url_is_rejected_without_writing(self):
        before=self.config.read_text(encoding='utf-8')
        response=self.post(llm_base_url='http://insecure.example.com/v1')
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.config.read_text(encoding='utf-8'),before)

    def test_empty_submission_is_rejected(self):
        response=self.post()
        self.assertEqual(response.status_code,400)
        self.assertIn('没有需要保存的修改',response.json()['detail'])

    def test_asr_endpoint_is_configurable(self):
        response=self.post(asr_base_url='https://asr.example.com/v1/audio/transcriptions')
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.section('asr')['base_url'],'https://asr.example.com/v1/audio/transcriptions')
        self.assertEqual(self.settings.asr_base_url,'https://asr.example.com/v1/audio/transcriptions')
        self.assertIn('asr.example.com',self.client.get('/admin').text)

    def test_insecure_asr_endpoint_is_rejected(self):
        before=self.config.read_text(encoding='utf-8')
        response=self.post(asr_base_url='http://asr.example.com/v1')
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.config.read_text(encoding='utf-8'),before)

    def test_asr_settings_take_effect_without_restart(self):
        response=self.post(asr_model='FunAudioLLM/SenseVoiceSmall',asr_key='asr-test-value')
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.section('asr')['model'],'FunAudioLLM/SenseVoiceSmall')
        self.assertEqual(self.section('asr')['api_key'],'asr-test-value')
        # 转写配置在调用时读取，保存后应立刻反映到运行中的设置。
        self.assertEqual(self.settings.asr_key,'asr-test-value')
        self.assertEqual(self.settings.get('asr','model'),'FunAudioLLM/SenseVoiceSmall')
        self.assertNotIn('asr-test-value',self.client.get('/admin').text)


if __name__=='__main__':unittest.main()
