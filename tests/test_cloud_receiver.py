import asyncio
import hashlib
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from cloud_receiver import Ledger, Receiver, UploadUncertain, upload, validate_glb
from echo3d_bridge.server import Relay, MAX_MESSAGE_BYTES, CHUNK_BYTES


def glb(padding=0):
    data = json.dumps({'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{}]}).encode()
    data += b' ' * ((-len(data)) % 4 + padding)
    return struct.pack('<4sIIII', b'glTF', 2, 20 + len(data), len(data), 0x4E4F534A) + data


def envelope(kind, **payload):
    return json.dumps(dict(type=kind, payload=payload))


class CloudUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tiny.glb'
        self.path.write_bytes(glb())
        self.creds = {'key': 'test-collection', 'email': 'test@example.invalid', 'userKey': 'not-a-real-secret', 'secKey': 'not-a-real-security-key'}

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_glb_hash(self):
        self.assertEqual(validate_glb(self.path), hashlib.sha256(glb()).hexdigest())

    def test_invalid_glb(self):
        for data in [b'', b'glTF', b'not a valid GLB file', glb()[:-1], glb() + b'junk']:
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                validate_glb(self.path)

    def test_invalid_chunk(self):
        data = bytearray(glb())
        struct.pack_into('<I', data, 16, 0x1234)
        self.path.write_bytes(data)
        with self.assertRaises(ValueError):
            validate_glb(self.path)

    def test_multipart_exact_fields_and_binary(self):
        calls = []
        def handler(request):
            body = request.read()
            calls.append(request)
            self.assertEqual(str(request.url), 'https://api.echo3d.com/upload')
            self.assertEqual(request.method, 'POST')
            self.assertIn('multipart/form-data', request.headers['Content-Type'])
            for key, value in {**self.creds, 'target_type': '2', 'noProcessingWait': 'true', 'allowDuplicate': 'false'}.items():
                self.assertIn(f'name="{key}"'.encode(), body)
                self.assertIn(value.encode(), body)
            self.assertIn(glb(), body)
            self.assertIn(b'filename="_cat.glb"', body)
            return httpx.Response(200, json={'id': 'asset-123'})
        self.assertEqual(upload(self.path, '../cat.glb', self.creds, httpx.MockTransport(handler)), 'asset-123')
        self.assertEqual(len(calls), 1)

    def test_http200_plain_text_is_not_success(self):
        for body in ['Key test-collection not found!', 'Processing did not complete in expected time. Your file was queued sucessfully and added to your collection.', 'OK']:
            with self.assertRaises(UploadUncertain):
                upload(self.path, 'cat.glb', self.creds, httpx.MockTransport(lambda r: httpx.Response(200, text=body)))

    def test_invalid_json_receipt(self):
        for data in [{}, [], {'id': ''}, {'id': 1}, {'id': 'bad\nreceipt'}, {'id': 'ok', 'error': 'bad key'}, {'error': 'bad key'}]:
            with self.assertRaises(UploadUncertain):
                upload(self.path, 'cat.glb', self.creds, httpx.MockTransport(lambda r: httpx.Response(200, json=data)))

    def test_http_errors_no_redirect_or_retries(self):
        for status in [302, 400, 401, 403, 429, 500]:
            calls = []
            def handler(r):
                calls.append(r)
                return httpx.Response(status, headers={'Location': 'https://example.invalid'}, text='not-a-real-secret')
            with self.assertRaises(UploadUncertain) as cm:
                upload(self.path, 'cat.glb', self.creds, httpx.MockTransport(handler))
            self.assertNotIn('not-a-real-secret', str(cm.exception))
            self.assertEqual(len(calls), 1)

    def test_timeout_not_retried(self):
        calls = []
        def handler(r):
            calls.append(r)
            raise httpx.ReadTimeout('not-a-real-secret', request=r)
        with self.assertRaises(UploadUncertain) as cm:
            upload(self.path, 'cat.glb', self.creds, httpx.MockTransport(handler))
        self.assertNotIn('not-a-real-secret', str(cm.exception))
        self.assertEqual(len(calls), 1)

    def test_response_limit(self):
        with self.assertRaises(UploadUncertain):
            upload(self.path, 'cat.glb', self.creds, httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * (1024 * 1024 + 1))))

    def test_ledger_persists(self):
        path = Path(self.temp.name) / 'receipts.sqlite3'
        ledger = Ledger(path)
        ledger.set('collection-a', 'delivery', 'sha', 'accepted', 'entry')
        ledger.close()
        ledger = Ledger(path)
        self.assertEqual(ledger.get('collection-a', 'delivery'), ('sha', 'accepted', 'entry'))
        self.assertIsNone(ledger.get('collection-b', 'delivery'))
        ledger.close()


class CloudIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / 'receipts.sqlite3')
        self.relay = Relay(inbox=self.root / 'inbox', receipt_timeout=2, import_timeout=3)
        self.server = await serve(self.relay.handle, '127.0.0.1', 0, max_size=MAX_MESSAGE_BYTES, compression=None)
        self.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.receiver_task = None
        self.calls = []

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        if self.receiver_task:
            await asyncio.wait_for(self.receiver_task, 3)
        self.ledger.close()
        self.temp.cleanup()

    def start_receiver(self, *, dry_run=False, fail=False):
        def uploader(path, name, credentials):
            self.calls.append((path.read_bytes(), name))
            if fail:
                raise UploadUncertain('Simulated uncertain timeout')
            return upload(path, name, {'key': 'fake', 'email': 'fake@example.invalid', 'userKey': 'fake'},
                          httpx.MockTransport(lambda r: httpx.Response(200, json={'id': 'asset-test'})))
        self.receiver = Receiver(self.ledger, 'scope', {}, dry_run=dry_run, uploader=uploader)
        self.receiver_task = asyncio.create_task(self.receiver.run(self.url + '/echo3d'))

    async def send_model(self, data, name='test.glb'):
        async with connect(self.url + '/', max_size=MAX_MESSAGE_BYTES) as ws:
            await ws.send(envelope('handshake', protocolVersion='1.1.0'))
            await ws.recv()
            total = (len(data) + CHUNK_BYTES - 1) // CHUNK_BYTES
            for index in range(total):
                part = data[index*CHUNK_BYTES:(index+1)*CHUNK_BYTES]
                await ws.send(envelope('file_transfer', fileId='studio-file', fileName=name, fileType='glb', chunkIndex=index, chunkTotal=total, chunkSize=len(part)).encode() + part)
                ack = json.loads(await ws.recv())
                self.assertTrue(ack['payload']['success'])
            for _ in range(3):
                await ws.recv()
        return next((self.root / 'inbox').glob('*/metadata.json'))

    async def wait_until(self, test):
        for _ in range(300):
            if test():
                return
            await asyncio.sleep(.01)
        self.fail('Condition did not become true')

    async def test_multichunk_cloud_accepts_and_bridge_delivered(self):
        self.start_receiver()
        data = glb(5 * 1024 * 1024)
        meta = await self.send_model(data)
        await self.wait_until(lambda: json.loads(meta.read_text())['delivered'])
        self.assertEqual(self.calls, [(data, 'test.glb')])
        delivery = meta.parent.name
        self.assertEqual(self.ledger.get('scope', delivery)[1:], ('accepted', 'asset-test'))

    async def test_redelivery_of_accepted_is_not_uploaded_again(self):
        meta = await self.send_model(glb())
        self.ledger.set('scope', meta.parent.name, hashlib.sha256(glb()).hexdigest(), 'accepted', 'old-asset')
        self.start_receiver()
        await self.wait_until(lambda: json.loads(meta.read_text())['delivered'])
        self.assertEqual(self.calls, [])

    async def test_uncertain_attempt_keeps_queue_and_blocks_retry(self):
        self.start_receiver(fail=True)
        meta = await self.send_model(glb())
        await self.wait_until(lambda: self.ledger.get('scope', meta.parent.name) is not None)
        await self.wait_until(lambda: self.receiver.current is None)
        self.assertFalse(json.loads(meta.read_text())['delivered'])
        self.assertEqual(self.ledger.get('scope', meta.parent.name)[1], 'uncertain')
        self.assertEqual(len(self.calls), 1)

    async def test_previous_uncertain_never_sends_request(self):
        meta = await self.send_model(glb())
        self.ledger.set('scope', meta.parent.name, hashlib.sha256(glb()).hexdigest(), 'uncertain')
        self.start_receiver()
        await self.wait_until(lambda: self.receiver.task is not None and self.receiver.task.done())
        self.assertEqual(self.calls, [])
        self.assertFalse(json.loads(meta.read_text())['delivered'])

    async def test_id_reused_with_different_bytes_is_blocked(self):
        meta = await self.send_model(glb())
        self.ledger.set('scope', meta.parent.name, 'different-hash', 'accepted', 'old-asset')
        self.start_receiver()
        await self.wait_until(lambda: self.receiver.task is not None and self.receiver.task.done())
        self.assertEqual(self.calls, [])
        self.assertFalse(json.loads(meta.read_text())['delivered'])

    async def test_dry_run_keeps_queue_and_never_calls_cloud(self):
        self.start_receiver(dry_run=True)
        meta = await self.send_model(glb())
        await self.wait_until(lambda: self.receiver.task is not None and self.receiver.task.done())
        self.assertEqual(self.calls, [])
        self.assertFalse(json.loads(meta.read_text())['delivered'])
        self.assertIsNone(self.ledger.get('scope', meta.parent.name))

    async def test_invalid_glb_keeps_queue_and_never_calls_cloud(self):
        self.start_receiver()
        meta = await self.send_model(b'glTF-but-invalid')
        await self.wait_until(lambda: self.receiver.task is not None and self.receiver.task.done())
        self.assertEqual(self.calls, [])
        self.assertFalse(json.loads(meta.read_text())['delivered'])

    async def test_socket_drop_during_http_preserves_receipt(self):
        started, release = threading.Event(), threading.Event()
        def uploader(path, name, credentials):
            started.set()
            release.wait(timeout=2)
            return 'accepted-after-disconnect'
        self.receiver = Receiver(self.ledger, 'scope', {}, uploader=uploader)
        self.receiver_task = asyncio.create_task(self.receiver.run(self.url + '/echo3d'))
        meta = await self.send_model(glb())
        await self.wait_until(started.is_set)
        await self.relay.peers['echo3d'].close()
        self.server.close()
        release.set()
        await self.server.wait_closed()
        await self.receiver_task
        self.assertEqual(self.ledger.get('scope', meta.parent.name)[1:], ('accepted', 'accepted-after-disconnect'))
        self.assertFalse(json.loads(meta.read_text())['delivered'])

    async def test_invalid_chunk_order_rejected(self):
        class Socket:
            async def send(self, value):
                raise AssertionError('Must not ACK invalid frame')
        receiver = Receiver(self.ledger, 'scope', {})
        raw = envelope('file_transfer', fileId='id', fileName='a.glb', fileType='glb', chunkIndex=1, chunkTotal=2, chunkSize=1).encode() + b'a'
        with self.assertRaises(ValueError):
            await receiver.frame(Socket(), raw)


if __name__ == '__main__':
    unittest.main()
