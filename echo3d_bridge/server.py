"""Local DCC WebSocket transport hosted by the desktop application."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import PROTOCOL_VERSION, __version__

PORT = 60710
CHUNK_BYTES = 5 * 1024 * 1024
HEADER_BYTES = 16 * 1024
MAX_MESSAGE_BYTES = CHUNK_BYTES + HEADER_BYTES
MAX_FILE_BYTES = 256 * 1024 * 1024


def parse_message(raw: str | bytes) -> tuple[dict, bytes]:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    depth, quoted, escaped = 0, False, False
    for index, value in enumerate(data[:HEADER_BYTES]):
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value == 123:
            depth += 1
        elif value == 125:
            depth -= 1
            if depth == 0:
                message = json.loads(data[:index + 1])
                if not isinstance(message, dict) or not isinstance(message.get("payload", {}), dict):
                    raise ValueError("Invalid message object")
                return message, data[index + 1:]
    raise ValueError("Missing JSON header or header exceeds 16 KiB")


@dataclass
class Transfer:
    file_id: str
    name: str
    total: int
    index: int = 0
    size: int = 0
    pending_size: int | None = None
    awaiting_result: bool = False
    timer: asyncio.TimerHandle | None = None


class Relay:
    def __init__(self, event_sink: Callable[[str, dict], None] | None = None,
                 receipt_timeout: float = 30, import_timeout: float = 300):
        self.peers: dict[str, Any] = {}
        self.transfer: Transfer | None = None
        self.event_sink = event_sink
        self.receipt_timeout = receipt_timeout
        self.import_timeout = import_timeout

    def emit(self, event: str, **payload: Any):
        if self.event_sink:
            self.event_sink(event, payload)

    @staticmethod
    async def send(socket: Any, kind: str, **payload: Any):
        await socket.send(json.dumps({"type": kind, "payload": payload}, ensure_ascii=False))

    def state(self):
        return {"bridgeConnected": True, "studioConnected": "studio" in self.peers,
                "echo3dConnected": "echo3d" in self.peers,
                "connected": "echo3d" in self.peers,
                "connectionState": "ready" if len(self.peers) == 2 else "waiting",
                "actionRequired": "" if "echo3d" in self.peers else "Connect echo3D frontend"}

    async def broadcast_state(self):
        self.emit("connections", **self.state())

    async def handle(self, socket):
        path = urlsplit(socket.request.path).path
        role = {"/": "studio", "/echo3d": "echo3d"}.get(path)
        if not role:
            await socket.close(1008, "Use / for Tripo Studio or /echo3d for echo3D")
            return
        registered = False
        try:
            raw = await asyncio.wait_for(socket.recv(), 10)
            msg, body = parse_message(raw)
            version = msg.get("payload", {}).get("protocolVersion")
            if msg.get("type") != "handshake" or body or version not in ("1.0.0", "1.1.0"):
                raise ValueError("Expected handshake with protocolVersion 1.0.0 or 1.1.0")
            if role in self.peers:
                raise ValueError(f"{role} is already connected; disconnect the other client first")
            self.peers[role] = socket
            registered = True
            await self.send(socket, "handshake_ack", success=True, clientName="echo3D",
                            dccVersion="Web", pluginVersion=__version__,
                            protocolVersion=PROTOCOL_VERSION, supportedFormats=["glb"], **self.state())
            await self.broadcast_state()
            async for raw in socket:
                payload = {}
                try:
                    msg, body = parse_message(raw)
                    kind, payload = msg.get("type"), msg.get("payload", {})
                    if kind == "ping" and not body:
                        await self.send(socket, "pong", **self.state())
                    elif role == "studio" and kind == "file_transfer" and isinstance(raw, bytes):
                        await self.forward_chunk(raw, payload, body)
                    elif role == "echo3d" and kind in ("file_transfer_ack", "file_transfer_complete", "import_complete", "file_loaded") and not body:
                        await self.receipt(kind, payload)
                    else:
                        raise ValueError("Unsupported message for this endpoint")
                except (ValueError, TypeError, KeyError) as exc:
                    await self.fail(str(exc), payload.get("fileId", ""))
                    await self.send(socket, "error", code=400, message=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            await self.send(socket, "handshake_ack", success=False, message=str(exc))
            await socket.close(1008, "Invalid handshake")
        except (asyncio.TimeoutError, ConnectionClosed):
            pass
        finally:
            if registered and self.peers.get(role) is socket:
                del self.peers[role]
                if self.transfer:
                    await self.fail(f"{role} disconnected; resend the model")
                await self.broadcast_state()

    async def forward_chunk(self, raw: bytes, p: dict, body: bytes):
        file_id, name = p.get("fileId"), p.get("fileName")
        index, total, size = p.get("chunkIndex"), p.get("chunkTotal"), p.get("chunkSize")
        if not isinstance(file_id, str) or not 1 <= len(file_id) <= 128:
            raise ValueError("Invalid fileId")
        if not isinstance(name, str) or not 1 <= len(name) <= 512 or p.get("fileType") != "glb":
            raise ValueError("Echo3D demo supports GLB files")
        if any(type(v) is not int for v in (index, total, size)) or not 0 <= index < total <= 1024:
            raise ValueError("Invalid chunk index or total")
        if size != len(body) or not 0 < size <= CHUNK_BYTES:
            raise ValueError("Chunk size mismatch or chunk exceeds 5 MiB")
        target = self.peers.get("echo3d")
        if target is None:
            raise ValueError("echo3D frontend is not connected")
        if self.transfer is None:
            if index != 0:
                raise ValueError("First chunk must have index 0")
            self.transfer = Transfer(file_id, name, total)
        t = self.transfer
        if (t.file_id, t.name, t.total, t.index) != (file_id, name, total, index) or t.pending_size is not None or t.awaiting_result:
            raise ValueError("Unexpected chunk; wait for matching ACK or restart transfer")
        if t.size + size > MAX_FILE_BYTES:
            raise ValueError("File exceeds 256 MiB")
        t.pending_size = size
        self.arm_timeout(t, self.receipt_timeout)
        self.emit("progress", fileName=t.name, percent=t.index * 100 / t.total, stage="传输中")
        try:
            await target.send(raw)
        except ConnectionClosed:
            await self.fail("echo3D disconnected; resend the model")

    def arm_timeout(self, t: Transfer, seconds: float):
        if t.timer:
            t.timer.cancel()
        async def expire():
            if self.transfer is t:
                await self.fail("echo3D receipt timed out; resend the model")
        t.timer = asyncio.get_running_loop().call_later(seconds, lambda: asyncio.create_task(expire()))

    async def receipt(self, kind: str, p: dict):
        t = self.transfer
        if t is None or p.get("fileId") != t.file_id:
            return
        source = self.peers.get("studio")
        if source is None:
            return
        if kind == "file_transfer_ack":
            if p.get("fileIndex") != t.index or t.pending_size is None or type(p.get("success")) is not bool:
                return
            if p["success"]:
                t.size += t.pending_size
                t.index += 1
                t.awaiting_result = t.index == t.total
            t.pending_size = None
            self.arm_timeout(t, self.import_timeout if t.awaiting_result else self.receipt_timeout)
            self.emit("progress", fileName=t.name, percent=t.index * 100 / t.total,
                      stage="等待 echo3D 处理结果" if t.awaiting_result else "传输中")
            await self.send(source, kind, **p)
        elif kind == "file_transfer_complete":
            if not t.awaiting_result:
                return
            await self.send(source, kind, **p)
            if p.get("status") != "success" and p.get("success") is not True:
                await self.fail(p.get("message", "echo3D assembly failed"))
        elif kind in ("import_complete", "file_loaded"):
            if not t.awaiting_result or type(p.get("success")) is not bool:
                return
            self.clear_transfer()
            result = dict(p)
            result.setdefault("errorMessage", p.get("message", ""))
            await self.send(source, "import_complete", **result)
            await self.send(source, "file_loaded", **result)
            self.emit("result", success=p["success"], fileName=t.name,
                      message=p.get("message", p.get("errorMessage", "")))

    def clear_transfer(self):
        if self.transfer and self.transfer.timer:
            self.transfer.timer.cancel()
        self.transfer = None

    async def fail(self, message: str, file_id: str = ""):
        t = self.transfer
        file_id = t.file_id if t else file_id
        self.clear_transfer()
        self.emit("result", success=False, fileName=t.name if t else "", message=message)
        result = dict(fileId=file_id, success=False, code=400, message=message, errorMessage=message)
        for role, socket in list(self.peers.items()):
            try:
                if role == "studio":
                    await self.send(socket, "import_complete", **result)
                    await self.send(socket, "file_loaded", **result)
                else:
                    await self.send(socket, "transfer_cancelled", **result)
            except ConnectionClosed:
                pass
