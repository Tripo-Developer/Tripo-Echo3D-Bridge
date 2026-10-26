import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from echo3d_bridge.server import Relay, parse_message, CHUNK_BYTES, MAX_MESSAGE_BYTES
from echo3d_bridge.settings import load_inbox, save_inbox


def envelope(kind, **payload):
    return json.dumps(dict(type=kind,payload=payload))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.temp.name)
        self.events = []
        self.relay = Relay(inbox=self.inbox, receipt_timeout=0.5,
                           event_sink=lambda event,p:self.events.append((event,p)))
        self.server = await serve(self.relay.handle,'127.0.0.1',0,max_size=MAX_MESSAGE_BYTES,compression=None)
        self.url='ws://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
        self.sockets=[]

    async def asyncTearDown(self):
        for ws in self.sockets: await ws.close()
        self.server.close(); await self.server.wait_closed()
        self.relay.clear_transfer()
        self.temp.cleanup()

    async def peer(self,role='studio'):
        ws=await connect(self.url+('/echo3d' if role=='echo3d' else '/'),max_size=MAX_MESSAGE_BYTES,origin='null')
        self.sockets.append(ws)
        await ws.send(envelope('handshake',protocolVersion='1.1.0').encode())
        ack=json.loads(await asyncio.wait_for(ws.recv(),2))
        self.assertTrue(ack['payload']['success']);self.assertTrue(ack['payload']['localStorageReady'])
        return ws

    async def send_chunk(self,ws,index,total,data,file_id='source-id',name='test}.glb'):
        raw=envelope('file_transfer',fileId=file_id,fileName=name,fileType='glb',chunkIndex=index,chunkTotal=total,chunkSize=len(data)).encode()+data
        await ws.send(raw)
        return json.loads(await asyncio.wait_for(ws.recv(),2))

    async def finish_source(self,ws):
        msgs=[json.loads(await asyncio.wait_for(ws.recv(),2)) for _ in range(3)]
        self.assertEqual([m['type'] for m in msgs],['file_transfer_complete','import_complete','file_loaded'])
        self.assertTrue(msgs[-1]['payload']['localSaved'])
        self.assertFalse(msgs[-1]['payload']['echo3dDelivered'])
        return Path(msgs[-1]['payload']['localPath'])

    async def receive_model(self,ws,expected,success=True):
        data=bytearray();file_id=None
        while len(data)<len(expected):
            msg,body=parse_message(await asyncio.wait_for(ws.recv(),3));p=msg['payload'];file_id=p['fileId']
            data.extend(body)
            await ws.send(envelope('file_transfer_ack',fileId=file_id,fileIndex=p['chunkIndex'],success=True))
        self.assertEqual(bytes(data),expected)
        await ws.send(envelope('file_transfer_complete',fileId=file_id,status='success'))
        await ws.send(envelope('import_complete',fileId=file_id,success=success,message='test receiver result'))
        for _ in range(100):
            if any(e=='delivery' and p['success']==success for e,p in self.events): break
            await asyncio.sleep(.01)
        self.assertTrue(any(e=='delivery' and p['success']==success for e,p in self.events))

    async def test_local_save_without_receiver_then_deliver_after_restart(self):
        studio=await self.peer()
        expected=b'glTF'+b'x'*(CHUNK_BYTES+13)
        for i in range(2):
            ack=await self.send_chunk(studio,i,2,expected[i*CHUNK_BYTES:(i+1)*CHUNK_BYTES])
            self.assertEqual(ack['type'],'file_transfer_ack');self.assertTrue(ack['payload']['success'])
        path=await self.finish_source(studio)
        self.assertEqual(path.read_bytes(),expected)
        await studio.close();self.server.close();await self.server.wait_closed()
        self.relay=Relay(inbox=self.inbox,event_sink=lambda e,p:self.events.append((e,p)))
        self.server=await serve(self.relay.handle,'127.0.0.1',0,max_size=MAX_MESSAGE_BYTES)
        self.url='ws://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
        echo=await self.peer('echo3d')
        await self.receive_model(echo,expected)
        self.assertEqual(path.read_bytes(),expected)
        self.assertEqual(self.relay.pending(),[])

    async def test_online_receiver_failure_keeps_local_success_and_file(self):
        echo=await self.peer('echo3d');studio=await self.peer()
        await self.send_chunk(studio,0,1,b'glTF')
        path=await self.finish_source(studio)
        await self.receive_model(echo,b'glTF',success=False)
        self.assertTrue(path.exists());self.assertEqual(len(self.relay.pending()),1)

    async def test_receiver_disconnect_does_not_interrupt_source(self):
        echo=await self.peer('echo3d');studio=await self.peer()
        self.assertTrue((await self.send_chunk(studio,0,2,b'glTF'))['payload']['success'])
        await echo.close()
        self.assertTrue((await self.send_chunk(studio,1,2,b'tail'))['payload']['success'])
        path=await self.finish_source(studio)
        self.assertEqual(path.read_bytes(),b'glTFtail')
        echo=await self.peer('echo3d');await self.receive_model(echo,b'glTFtail')

    async def test_unfinished_source_disconnect_cleanup_and_safe_filename(self):
        studio=await self.peer()
        await self.send_chunk(studio,0,2,b'partial')
        await studio.close()
        for _ in range(100):
            if self.relay.transfer is None: break
            await asyncio.sleep(.01)
        self.assertEqual(list(self.inbox.rglob('*.part')),[])
        self.assertEqual(self.relay.pending(),[])
        studio=await self.peer()
        await self.send_chunk(studio,0,1,b'glTF',name='../../CON.glb')
        path=await self.finish_source(studio)
        self.assertTrue(path.is_relative_to(self.inbox));self.assertEqual(path.read_bytes(),b'glTF')

    async def test_disk_error_never_sends_success_ack(self):
        studio=await self.peer()
        with patch.object(self.relay,'append_chunk',side_effect=OSError('Disk full')):
            result=await self.send_chunk(studio,0,1,b'glTF')
        self.assertEqual(result['type'],'import_complete')
        self.assertFalse(result['payload']['success'])
        self.assertIn('Disk full',result['payload']['message'])
        self.assertEqual(self.relay.pending(),[])

    async def test_json_prefix_with_escaped_brace(self):
        msg,body=parse_message(b'{"type":"file_transfer","payload":{"fileName":"a}.glb"}}abc')
        self.assertEqual(body,b'abc')

    async def test_custom_inbox_persists_new_writes_and_old_queue(self):
        settings = self.inbox / 'settings.json'
        custom = self.inbox / 'custom cache'
        studio = await self.peer()
        await self.send_chunk(studio, 0, 1, b'old')
        old_model = await self.finish_source(studio)
        await self.relay.change_inbox(custom, lambda p: save_inbox(p, settings))
        self.assertEqual(load_inbox(settings), custom.resolve())
        self.assertTrue(old_model.exists())
        self.assertEqual(self.relay.pending(), [])
        await self.send_chunk(studio, 0, 1, b'new')
        new_model = await self.finish_source(studio)
        self.assertTrue(new_model.is_relative_to(custom))
        self.assertEqual(new_model.read_bytes(), b'new')
        restarted = Relay(inbox=load_inbox(settings))
        self.assertEqual(restarted.pending()[0][1], new_model)
        await self.relay.change_inbox(self.inbox, lambda p: save_inbox(p, settings))
        self.assertEqual(self.relay.pending()[0][1], old_model)

    async def test_cache_change_rejects_active_transfer_and_failed_config(self):
        studio = await self.peer()
        await self.send_chunk(studio, 0, 2, b'first')
        saved = []
        with self.assertRaisesRegex(ValueError, 'current transfer to finish'):
            await self.relay.change_inbox(self.inbox/'new', saved.append)
        self.assertEqual(saved, [])
        self.assertEqual(self.relay.inbox, self.inbox)
        await self.send_chunk(studio, 1, 2, b'last')
        model = await self.finish_source(studio)
        self.assertEqual(model.read_bytes(), b'firstlast')
        def denied(path):
            raise PermissionError('configuration is read-only')
        with self.assertRaises(PermissionError):
            await self.relay.change_inbox(self.inbox/'new', denied)
        self.assertEqual(self.relay.inbox, self.inbox)
        with self.assertRaises(OSError):
            await self.relay.change_inbox(model, saved.append)
        self.assertEqual(self.relay.inbox, self.inbox)
        async with self.relay.delivery_lock:
            with self.assertRaisesRegex(ValueError, 'current transfer to finish'):
                await self.relay.change_inbox(self.inbox/'new', saved.append)

if __name__=='__main__': unittest.main()
