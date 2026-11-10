"""Focused error-reporting regression tests for the original receiver flow."""
import json
import tempfile
import unittest
from pathlib import Path
import httpx
from cloud_receiver import upload, UploadUncertain, safe_error_detail, bridge_failure_message
from test_cloud_receiver import glb

class ErrorDetailTests(unittest.TestCase):
    def test_json_error_retains_useful_detail(self):
        raw = b'{"error":"Duplicate content", "message":"This model already exists"}'
        self.assertIn('Duplicate content', safe_error_detail(raw, {}))
    def test_redacts_known_keys_and_labeled_secrets(self):
        credentials = {'key':'private-collection', 'userKey':'private-user', 'secKey':'private-security','email':'ben@example.invalid'}
        for raw in [json.dumps(dict(credentials, error='Duplicate content')).encode(), b'Invalid key=private-collection userKey: private-user secKey=private-security', b'URL https://api.echo3D.com/query?key=private-collection&userKey=private-user', b'{"authorization":"Bearer unrelated-token","password":"unrelated-password","message":"Bad credentials"}']:
            text = safe_error_detail(raw, credentials)
            for secret in list(credentials.values())+['unrelated-token','unrelated-password']:
                self.assertNotIn(secret, text)
    def test_control_characters_and_length_bounded(self):
        s = safe_error_detail(b'\x1b[31mDuplicate\x00\r\n'+b'x'*3000, {})
        self.assertNotIn('\x1b',s)
        self.assertNotIn('\n',s)
        self.assertLess(len(s),1300)
    def test_http_error_prints_actual_redacted_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.glb';p.write_bytes(glb())
            calls=[]
            def handler(request):
                calls.append(request)
                return httpx.Response(400,json={'error':'Duplicate content','key':'private-collection'})
            with self.assertRaises(UploadUncertain) as caught:
                upload(p,'a.glb',{'key':'private-collection'},httpx.MockTransport(handler))
            self.assertIn('HTTP 400',str(caught.exception))
            self.assertIn('Duplicate content',str(caught.exception))
            self.assertNotIn('private-collection',str(caught.exception))
            self.assertEqual(len(calls),1)
    def test_bridge_failure_message_keeps_detail_bounded_single_line(self):
        text = bridge_failure_message(UploadUncertain('HTTP 400; echo3D: {"error": "Duplicate content"}; inspect the collection before retrying'))
        self.assertIn('Duplicate content', text)
        self.assertNotIn('\n', text)
        self.assertLessEqual(len(text), 920)
    def test_bridge_failure_message_truncates_and_handles_blank(self):
        long_text = bridge_failure_message(UploadUncertain('x' * 5000))
        self.assertTrue(long_text.endswith('[truncated]'))
        self.assertLessEqual(len(long_text), 920)
        self.assertEqual(bridge_failure_message(ValueError('  ')), 'Upload failed without a diagnostic; inspect receiver terminal')
    def test_http200_error_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.glb';p.write_bytes(glb())
            with self.assertRaisesRegex(UploadUncertain,'Collection requires metadata'):
                upload(p,'a.glb',{},httpx.MockTransport(lambda r:httpx.Response(200,json={'error':'Collection requires metadata'})))
