"""Real loopback HTTP + WebSocket checks. No echo3D requests or credits."""
import asyncio
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from websockets.asyncio.server import serve
import cloud_receiver as cr
from echo3d_bridge.server import Relay, MAX_MESSAGE_BYTES
from test_cloud_receiver import glb

class LoopbackFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_original_queue_flow_error_then_success_real_sockets(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers['Content-Length'])))
                status=400 if len(requests)==1 else 200
                body=json.dumps({'error':'Duplicate content','key':'secret-test'} if status==400 else {'id':'loopback-accepted'}).encode()
                self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);inbox=root/'inbox';inbox.mkdir()
                metas=[]
                for i in range(2):
                    folder=inbox/str(i);folder.mkdir();data=glb(i*4)
                    (folder/'model-test.glb').write_bytes(data)
                    meta=folder/'metadata.json';meta.write_text(json.dumps({'deliveryId':str(i),'fileName':'test.glb','storedName':'model-test.glb','size':len(data),'delivered':False}));metas.append(meta)
                ledger=cr.Ledger(root/'receipts.sqlite3')
                events=[]
                relay=Relay(inbox=inbox,receipt_timeout=2,import_timeout=3,event_sink=lambda event,payload:events.append((event,payload)))
                server=await serve(relay.handle,'127.0.0.1',0,max_size=MAX_MESSAGE_BYTES,compression=None)
                url='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/echo3d'
                output=io.StringIO()
                with patch.object(cr,'API_URL','http://127.0.0.1:'+str(http.server_port)+'/upload'),contextlib.redirect_stdout(output):
                    task=asyncio.create_task(cr.Receiver(ledger,'scope',{'key':'secret-test'}).run(url))
                    for _ in range(400):
                        if any(json.loads(m.read_text())['delivered'] for m in metas):break
                        await asyncio.sleep(.01)
                    self.assertEqual(sorted(json.loads(m.read_text())['delivered'] for m in metas),[False,True])
                    server.close();await server.wait_closed();await task
                self.assertEqual(sorted(ledger.get('scope',str(i))[1] for i in range(2)),['accepted','uncertain'])
                self.assertEqual([ledger.get('scope',str(i))[2] for i in range(2) if ledger.get('scope',str(i))[1]=='accepted'],['loopback-accepted'])
                self.assertEqual(len(requests),2)
                self.assertIn('Duplicate content',output.getvalue())
                self.assertNotIn('secret-test',output.getvalue())
                # The Bridge panel/log line is the emitted delivery event message.
                deliveries=[p for e,p in events if e=='delivery']
                self.assertEqual([p['success'] for p in deliveries],[False,True])
                self.assertIn('Delivery failed:',deliveries[0]['message'])
                self.assertIn('Duplicate content',deliveries[0]['message'])
                self.assertNotIn('secret-test',deliveries[0]['message'])
                self.assertNotIn('Upload not confirmed; inspect receiver terminal',deliveries[0]['message'])
                self.assertTrue(all((m.parent/'model-test.glb').exists() for m in metas))
                ledger.close()
        finally:
            http.shutdown();thread.join();http.server_close()
