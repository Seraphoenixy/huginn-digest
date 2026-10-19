import json
import socket
import unittest
from unittest.mock import patch, Mock
from integration.http.public import public_url, fetch
from integration.http.llm import complete, CloudError
from integration.sources import rss
from model.asr import CloudASR

class ConnectorTests(unittest.TestCase):
    def test_reject_private_and_invalid_addresses(self):
        for address in ['127.0.0.1','10.0.0.1','169.254.169.254','198.18.0.1','::1']:
            with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]):
                with self.assertRaises(ValueError):public_url('https://example.com/')
        for address in ['file:///etc/passwd','https://user:password@example.com/','http://example.com:8000/']:
            with self.assertRaises(ValueError):public_url(address)

    def test_redirect_revalidated_and_session_closed(self):
        session=Mock();response=Mock(status_code=302,headers={'Location':'http://127.0.0.1/'})
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session.get.return_value=response
        with patch('integration.http.public.requests.Session',return_value=session), patch('integration.http.public.public_url',side_effect=['https://example.com',ValueError('blocked')]):
            with self.assertRaises(ValueError):fetch('https://example.com')
        session.close.assert_called_once()

    def test_rss_discovery_and_article_fallback(self):
        feed=b'''<rss version="2.0"><channel><title>News</title><item><title>Story</title><link>https://example.com/story</link><pubDate>Sun, 18 Oct 2026 01:00:00 GMT</pubDate><description>Facts from original feed</description></item></channel></rss>'''
        with patch.object(rss,'fetch',return_value=feed),patch.object(rss,'public_url',side_effect=lambda u:u):
            rows=rss.discover('https://example.com/rss',0)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['title'],'Story')
        self.assertGreater(rows[0]['published'],0)
        with patch.object(rss,'fetch',side_effect=RuntimeError('unavailable')):
            text,warning=rss.extract(rows[0]['url'],rows[0]['body'])
        self.assertIn('Facts',text)
        self.assertIn('信息可能不完整',warning)

    def test_llm_rejects_empty_truncated_and_http_errors(self):
        settings=Mock(llm_key='test')
        settings.get.side_effect=lambda section,key,default=None:{'model':'test','base_url':'https://example.com/v1','timeout':10,'thinking_disabled':True}.get(key,default)
        for code,choice in [(401,{}),(200,{'finish_reason':'length','message':{'content':'partial'}}),(200,{'finish_reason':'stop','message':{'content':''}})]:
            response=Mock(status_code=code)
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.json.return_value={'choices':[choice]}
            with patch('integration.http.llm.requests.post',return_value=response):
                with self.assertRaises(CloudError):complete(settings,[])

    def test_llm_retry_and_thinking_payload(self):
        settings=Mock(llm_key='test')
        settings.get.side_effect=lambda section,key,default=None:{'model':'test','base_url':'https://example.com/v1','timeout':10,'thinking_disabled':True}.get(key,default)
        responses=[]
        for code in [429,200]:
            response=Mock(status_code=code)
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}]}
            responses.append(response)
        with patch('integration.http.llm.requests.post',side_effect=responses) as post,patch('integration.http.llm.time.sleep'):
            self.assertEqual(complete(settings,[]),'ok')
        self.assertEqual(post.call_args.kwargs['json']['thinking'],{'type':'disabled'})

if __name__=='__main__':unittest.main()
