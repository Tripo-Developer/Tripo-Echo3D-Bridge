import asyncio
import json
import unittest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from echo3d_bridge.server import Relay, parse_message

def envelope(kind, payload):
    return json.dumps({"type": kind, "payload": payload})

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_balanced_json_header(self):
        msg, body = parse_message(b'{"type":"file_transfer","payload":{"fileName":"a}.glb"}}abc')
        self.assertEqual(msg["type"], "file_transfer")
        self.assertEqual(body, b"abc")

    async def test_initial_state(self):
        relay = Relay()
        self.assertFalse(relay.state()["studioConnected"])
        self.assertFalse(relay.state()["echo3dConnected"])

    async def test_existing_dcc_protocol_and_acknowledged_transfer(self):
        relay = Relay()
        async with serve(relay.handle, "127.0.0.1", 0, max_size=6 * 1024 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/") as studio, \
                    connect(f"ws://127.0.0.1:{port}/echo3d") as echo:
                hello = envelope("handshake", {"clientName": "Tripo Studio", "protocolVersion": "1.0.0"})
                await studio.send(hello)
                studio_ack = json.loads(await studio.recv())
                self.assertEqual(studio_ack["type"], "handshake_ack")
                self.assertTrue(studio_ack["payload"]["success"])
                await echo.send(hello)
                echo_ack = json.loads(await echo.recv())
                self.assertTrue(echo_ack["payload"]["success"])

                await studio.send(envelope("ping", {}))
                self.assertEqual(json.loads(await studio.recv())["type"], "pong")

                file_id, chunk = "file_123", b"glTF-demo"
                header = envelope("file_transfer", {"fileId": file_id, "fileName": "demo.glb",
                    "fileType": "glb", "chunkIndex": 0, "chunkTotal": 1, "chunkSize": len(chunk)})
                await studio.send(header.encode() + chunk)
                received, body = parse_message(await echo.recv())
                self.assertEqual(received["payload"]["fileId"], file_id)
                self.assertEqual(body, chunk)

                await echo.send(envelope("file_transfer_ack", {"fileId": file_id, "fileIndex": 0, "success": True}))
                self.assertEqual(json.loads(await studio.recv())["type"], "file_transfer_ack")
                await echo.send(envelope("file_transfer_complete", {"fileId": file_id, "status": "success"}))
                self.assertEqual(json.loads(await studio.recv())["type"], "file_transfer_complete")
                await echo.send(envelope("file_loaded", {"fileId": file_id, "success": True}))
                self.assertEqual(json.loads(await studio.recv())["type"], "import_complete")
                self.assertEqual(json.loads(await studio.recv())["type"], "file_loaded")
                self.assertIsNone(relay.transfer)

    async def test_multichunk_retry_timeout_and_reconnect(self):
        relay = Relay(receipt_timeout=0.4)
        async with serve(relay.handle, "127.0.0.1", 0, max_size=6 * 1024 * 1024, compression=None) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/", origin="null") as studio:
                await studio.send(envelope("handshake", {"protocolVersion": "1.1.0"}).encode())
                self.assertTrue(json.loads(await studio.recv())["payload"]["success"])
                async def chunk(index, data, file_id="large"):
                    raw = envelope("file_transfer", {"fileId":file_id,"fileName":"中文}.glb","fileType":"glb",
                        "chunkIndex":index,"chunkTotal":2,"chunkSize":len(data)}).encode()+data
                    await studio.send(raw)
                    return raw
                await chunk(0,b"test", "missing")
                self.assertFalse(json.loads(await studio.recv())["payload"]["success"])
                await studio.recv() # file_loaded
                await studio.recv() # error
                async with connect(f"ws://127.0.0.1:{port}/echo3d",max_size=6*1024*1024) as echo:
                    await echo.send(envelope("handshake", {"protocolVersion":"1.0.0"}))
                    await echo.recv()
                    raw = await chunk(0,b"x"*(5*1024*1024))
                    self.assertEqual(await echo.recv(),raw)
                    await echo.send(envelope("file_transfer_ack", {"fileId":"large","fileIndex":0,"success":False}))
                    self.assertFalse(json.loads(await studio.recv())["payload"]["success"])
                    raw = await chunk(0,b"x"*(5*1024*1024))
                    self.assertEqual(await echo.recv(),raw)
                    await echo.send(envelope("file_transfer_ack", {"fileId":"large","fileIndex":0,"success":True}))
                    await studio.recv()
                    raw = await chunk(1,b"tail")
                    self.assertEqual(await echo.recv(),raw)
                    # A stale ACK cannot advance the second chunk.
                    await echo.send(envelope("file_transfer_ack", {"fileId":"large","fileIndex":0,"success":True}))
                    await echo.send(envelope("ping", {})); await echo.recv()
                    self.assertEqual(relay.transfer.index,1)
                    # Disconnect must fail the in-flight model and release the role.
                result=json.loads(await studio.recv())
                self.assertFalse(result['payload']['success'])
                await studio.recv()
                async with connect(f"ws://127.0.0.1:{port}/echo3d") as echo:
                    await echo.send(envelope("handshake", {"protocolVersion":"1.0.0"}))
                    self.assertTrue(json.loads(await echo.recv())["payload"]["success"])
                    await chunk(0,b"timeout", "new")
                    await echo.recv()
                    result=json.loads(await asyncio.wait_for(studio.recv(), 2))
                    self.assertIn('timed out',result['payload']['message'])
                    self.assertIsNone(relay.transfer)

if __name__ == "__main__":
    unittest.main()
