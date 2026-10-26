"""Disk-backed DCC inbox and independent WebSocket delivery to Echo3D."""
from __future__ import annotations
import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from . import PROTOCOL_VERSION, __version__
from .settings import DEFAULT_INBOX, validate_inbox, save_inbox

PORT = 60710
CHUNK_BYTES = 5 * 1024 * 1024
HEADER_BYTES = 16 * 1024
MAX_MESSAGE_BYTES = CHUNK_BYTES + HEADER_BYTES
MAX_FILE_BYTES = 256 * 1024 * 1024


def parse_message(raw: str | bytes) -> tuple[dict, bytes]:
    data = raw.encode('utf-8') if isinstance(raw, str) else raw
    depth, quoted, escaped = 0, False, False
    for index, value in enumerate(data[:HEADER_BYTES]):
        if quoted:
            if escaped: escaped = False
            elif value == 92: escaped = True
            elif value == 34: quoted = False
        elif value == 34: quoted = True
        elif value == 123: depth += 1
        elif value == 125:
            depth -= 1
            if depth == 0:
                message = json.loads(data[:index + 1])
                if not isinstance(message, dict) or not isinstance(message.get('payload', {}), dict):
                    raise ValueError('Invalid message object')
                return message, data[index + 1:]
    raise ValueError('Missing JSON header or header exceeds 16 KiB')


@dataclass
class Transfer:
    file_id: str
    name: str
    total: int
    folder: Path
    index: int = 0
    size: int = 0
    timer: asyncio.TimerHandle | None = None


class Relay:
    def __init__(self, event_sink: Callable[[str, dict], None] | None = None,
                 receipt_timeout: float = 30, import_timeout: float = 300,
                 inbox: Path | None = None):
        self.peers: dict[str, Any] = {}
        self.transfer: Transfer | None = None
        self.event_sink = event_sink
        self.receipt_timeout, self.import_timeout = receipt_timeout, import_timeout
        self.inbox = Path(inbox or DEFAULT_INBOX).resolve()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.wake = asyncio.Event()
        self.receipts: asyncio.Queue | None = None
        self.delivery_task: asyncio.Task | None = None
        self.io_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()

    async def change_inbox(self, path: Path, persist=save_inbox):
        candidate = await asyncio.to_thread(validate_inbox, path)
        async with self.io_lock:
            if self.transfer is not None or self.delivery_lock.locked():
                raise ValueError('Wait for the current transfer to finish before changing the cache folder.')
            async with self.delivery_lock:
                # Persist first: a settings write error must leave the active inbox intact.
                await asyncio.to_thread(persist, candidate)
                self.inbox = candidate
                self.wake.set()
        return candidate

    def emit(self, event: str, **payload):
        if self.event_sink: self.event_sink(event, payload)

    @staticmethod
    async def send(socket, kind: str, **payload):
        await socket.send(json.dumps({'type': kind, 'payload': payload}, ensure_ascii=False))

    def state(self):
        return dict(bridgeConnected=True, studioConnected='studio' in self.peers,
                    echo3dConnected='echo3d' in self.peers, connected=True,
                    localStorageReady=True, connectionState='ready', actionRequired='')

    async def broadcast_state(self):
        self.emit('connections', **self.state())

    async def handle(self, socket):
        role = {'/': 'studio', '/echo3d': 'echo3d'}.get(urlsplit(socket.request.path).path)
        if not role:
            await socket.close(1008, 'Use / or /echo3d'); return
        registered = False
        worker = None
        try:
            msg, body = parse_message(await asyncio.wait_for(socket.recv(), 10))
            if msg.get('type') != 'handshake' or body or msg.get('payload', {}).get('protocolVersion') not in ('1.0.0', '1.1.0'):
                raise ValueError('Expected handshake protocolVersion 1.0.0 or 1.1.0')
            if role in self.peers: raise ValueError(f'{role} is already connected')
            self.peers[role] = socket
            registered = True
            await self.send(socket, 'handshake_ack', success=True, clientName='echo3D', dccVersion='Web demo',
                            pluginVersion=__version__, protocolVersion=PROTOCOL_VERSION, supportedFormats=['glb'], **self.state())
            await self.broadcast_state()
            if role == 'echo3d':
                self.receipts = asyncio.Queue(maxsize=64)
                worker = self.delivery_task = asyncio.create_task(self.deliver_pending(socket, self.receipts))
            async for raw in socket:
                p = {}
                try:
                    msg, body = parse_message(raw)
                    kind, p = msg.get('type'), msg.get('payload', {})
                    if kind == 'ping' and not body:
                        await self.send(socket, 'pong', **self.state())
                    elif role == 'studio' and kind == 'file_transfer' and isinstance(raw, bytes):
                        async with self.io_lock:
                            await self.receive_chunk(socket, p, body)
                    elif role == 'echo3d' and kind in ('file_transfer_ack','file_transfer_complete','import_complete','file_loaded') and not body:
                        if self.receipts and not self.receipts.full(): self.receipts.put_nowait((kind,p))
                    else: raise ValueError('Unsupported message for this endpoint')
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    if role == 'studio': await self.fail(str(exc), p.get('fileId', ''))
                    await self.send(socket, 'error', code=400, message=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            await self.send(socket,'handshake_ack',success=False,message=str(exc))
            await socket.close(1008,'Invalid handshake')
        except (asyncio.TimeoutError, ConnectionClosed): pass
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if registered and self.peers.get(role) is socket:
                del self.peers[role]
                if role == 'studio' and self.transfer:
                    await self.fail('Tripo disconnected before local save completed; resend model')
                await self.broadcast_state()

    async def receive_chunk(self, source, p, body):
        file_id, name = p.get('fileId'), p.get('fileName')
        index, total, size = p.get('chunkIndex'), p.get('chunkTotal'), p.get('chunkSize', len(body))
        if not isinstance(file_id,str) or not 1 <= len(file_id) <= 128: raise ValueError('Invalid fileId')
        if not isinstance(name,str) or not 1 <= len(name) <= 512 or p.get('fileType') != 'glb': raise ValueError('Only GLB is supported')
        if any(type(v) is not int for v in (index,total,size)) or not 0 <= index < total <= 1024: raise ValueError('Invalid chunk index or total')
        if size != len(body) or not 0 < size <= CHUNK_BYTES: raise ValueError('Invalid chunk size (maximum 5 MiB)')
        if self.transfer is None:
            if index != 0: raise ValueError('First chunk must be index 0')
            folder = self.inbox / uuid.uuid4().hex
            folder.mkdir()
            self.transfer = Transfer(file_id,name,total,folder)
        t = self.transfer
        if (file_id,name,total,index) != (t.file_id,t.name,t.total,t.index): raise ValueError('Unexpected chunk metadata or index')
        if t.size + size > MAX_FILE_BYTES: raise ValueError('Model exceeds 256 MiB')
        if t.timer: t.timer.cancel()
        await asyncio.to_thread(self.append_chunk,t.folder/'model.part',body)
        t.size += size; t.index += 1
        if t.index == t.total:
            # Commit complete bytes and metadata before acknowledging the last chunk.
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:160] or 'model'
            if not safe_name.lower().endswith('.glb'): safe_name += '.glb'
            safe_name = 'model-' + safe_name  # Avoid Windows reserved device names.
            model = t.folder / safe_name
            await asyncio.to_thread((t.folder/'model.part').replace,model)
            meta = dict(fileId=t.file_id,fileName=t.name,fileType='glb',size=t.size,
                        storedName=safe_name,deliveryId=t.folder.name,delivered=False)
            await asyncio.to_thread(self.write_meta,t.folder,meta)
            self.transfer = None
            self.wake.set()
            await self.send(source,'file_transfer_ack',fileId=file_id,fileIndex=index,success=True)
            await self.send(source,'file_transfer_complete',fileId=file_id,status='success',localPath=str(model))
            result = dict(fileId=file_id,success=True,localSaved=True,echo3dDelivered=False,
                          localPath=str(model),message='Saved locally. Awaiting echo3D.',completionStage='local_saved')
            await self.send(source,'import_complete',**result)
            await self.send(source,'file_loaded',**result)
            self.emit('result',fileName=name,**result)
        else:
            self.arm_timeout(t)
            await self.send(source,'file_transfer_ack',fileId=file_id,fileIndex=index,success=True)
            self.emit('progress',fileName=name,percent=t.index*100/t.total,stage='Saving')

    @staticmethod
    def append_chunk(path, body):
        with path.open('ab') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())

    @staticmethod
    def write_meta(folder, meta):
        temporary = folder/'metadata.tmp'
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(meta,stream,ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(folder/'metadata.json')

    def pending(self):
        items=[]
        for path in sorted(self.inbox.glob('*/metadata.json'),key=lambda p:p.stat().st_mtime):
            try:
                meta=json.loads(path.read_text(encoding='utf-8'))
                model=(path.parent/meta['storedName']).resolve()
                if model.parent != path.parent.resolve() or not model.is_file(): continue
                if not meta.get('delivered'): items.append((path.parent,model,meta))
            except (OSError,ValueError,KeyError,TypeError): continue
        return items

    async def deliver_pending(self, target, receipts):
        try:
            while True:
                self.wake.clear()
                async with self.delivery_lock:
                    for folder,model,meta in await asyncio.to_thread(self.pending):
                        try:
                            await self.deliver_one(target,receipts,model,meta)
                            meta['delivered']=True
                            await asyncio.to_thread(self.write_meta,folder,meta)
                            self.emit('delivery',success=True,fileName=meta['fileName'],localPath=str(model),message='Delivered to echo3D.')
                        except (asyncio.TimeoutError,ValueError,OSError) as exc:
                            self.emit('delivery',success=False,fileName=meta['fileName'],localPath=str(model),message=f'Delivery failed: {str(exc) or "Acknowledgment timeout"}. Reconnect echo3D to retry.')
                            await self.send(target,'transfer_cancelled',fileId=meta['deliveryId'],message='Delivery failed; reconnect to retry')
                await self.wake.wait()
        except ConnectionClosed: pass

    async def wait_receipt(self,receipts,kind,file_id,index=None,timeout=None):
        async def wait():
            while True:
                received,p = await receipts.get()
                if p.get('fileId') != file_id: continue
                if received not in (kind, 'file_loaded' if kind=='import_complete' else kind): continue
                if index is not None and p.get('fileIndex') != index: continue
                if p.get('success') is not True: raise ValueError(p.get('message','Receiver rejected model'))
                return p
        return await asyncio.wait_for(wait(),timeout or self.receipt_timeout)

    async def deliver_one(self,target,receipts,model,meta):
        file_id=meta['deliveryId']; total=(meta['size']+CHUNK_BYTES-1)//CHUNK_BYTES
        with model.open('rb') as stream:
            for index in range(total):
                body=await asyncio.to_thread(stream.read,CHUNK_BYTES)
                header=dict(type='file_transfer',payload=dict(fileId=file_id,fileName=meta['fileName'],fileType='glb',chunkIndex=index,chunkTotal=total,chunkSize=len(body)))
                await target.send(json.dumps(header,ensure_ascii=False).encode()+body)
                await self.wait_receipt(receipts,'file_transfer_ack',file_id,index)
                self.emit('progress',fileName=meta['fileName'],percent=(index+1)*100/total,stage='Sending to echo3D')
        await self.wait_receipt(receipts,'import_complete',file_id,timeout=self.import_timeout)

    def arm_timeout(self,t):
        async def expire():
            async with self.io_lock:
                if self.transfer is t: await self.fail('Tripo chunk timeout; resend model')
        t.timer=asyncio.get_running_loop().call_later(self.receipt_timeout,lambda:asyncio.create_task(expire()))

    def clear_transfer(self):
        t=self.transfer; self.transfer=None
        if t:
            if t.timer: t.timer.cancel()
            (t.folder/'model.part').unlink(missing_ok=True)
            # Only remove our empty unfinished directory; completed model files remain.
            if not any(t.folder.iterdir()): t.folder.rmdir()

    async def fail(self,message,file_id=''):
        t=self.transfer
        if t: file_id=t.file_id
        self.clear_transfer()
        result=dict(fileId=file_id,success=False,code=400,message=message,errorMessage=message)
        self.emit('result',fileName=t.name if t else '',**result)
        source=self.peers.get('studio')
        if source:
            try:
                await self.send(source,'import_complete',**result)
                await self.send(source,'file_loaded',**result)
            except ConnectionClosed: pass
