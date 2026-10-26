"""Exercise actual bind conflicts, retry recovery and interruptible shutdown."""
import asyncio
import errno
import json
import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.client import connect

from desktop_app import RelayRuntime, is_port_in_use, serve_demo


class RuntimeTests(unittest.TestCase):
    def wait_event(self, events, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event, payload = events.get(timeout=max(.01, deadline - time.monotonic()))
            self.assertNotEqual(event, 'error', payload)
            if event == name:
                return payload
        self.fail(f'No {name} event')

    def occupied(self, family, host):
        blocker = socket.socket(family)
        self.addCleanup(blocker.close)
        if family == socket.AF_INET6:
            blocker.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        blocker.bind((host, 0))
        blocker.listen()
        return blocker, blocker.getsockname()[1]

    def recover(self, family, host):
        blocker, port = self.occupied(family, host)
        with tempfile.TemporaryDirectory() as folder:
            events = queue.Queue()
            runtime = RelayRuntime(events, Path(folder), port=port, retry_interval=.05)
            runtime.start()
            try:
                self.assertEqual(self.wait_event(events, 'port_in_use')['port'], port)
                time.sleep(.2)
                self.assertTrue(events.empty(), 'Do not flood logs on every retry')
                if family == socket.AF_INET6:
                    # IPv4 bound first, but must not be left listening after IPv6 fails.
                    with socket.socket() as probe:
                        probe.bind(('127.0.0.1', port))
                blocker.close()
                self.wait_event(events, 'ready')

                async def handshake():
                    async with connect(f'ws://127.0.0.1:{port}/', proxy=None) as ws:
                        await ws.send(json.dumps({'type': 'handshake', 'payload': {'protocolVersion': '1.1.0'}}))
                        reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        self.assertTrue(reply['payload']['success'])
                asyncio.run(handshake())
            finally:
                runtime.stop()
            self.assertFalse(runtime.thread.is_alive())
            with socket.socket(family) as probe:
                probe.bind((host, port))

    def test_ipv4_conflict_then_release_and_handshake(self):
        self.recover(socket.AF_INET, '127.0.0.1')

    @unittest.skipUnless(socket.has_ipv6, 'IPv6 unavailable')
    def test_ipv6_conflict_releases_partial_bind_and_recovers(self):
        self.recover(socket.AF_INET6, '::1')

    def test_close_interrupts_retry_wait(self):
        blocker, port = self.occupied(socket.AF_INET, '127.0.0.1')
        with tempfile.TemporaryDirectory() as folder:
            events = queue.Queue()
            runtime = RelayRuntime(events, Path(folder), port=port, retry_interval=30)
            runtime.start()
            try:
                self.wait_event(events, 'port_in_use')
                start = time.monotonic()
                runtime.stop()
                self.assertLess(time.monotonic() - start, 1)
                self.assertFalse(runtime.thread.is_alive())
                blocker.close()
                self.assertTrue(events.empty())
            finally:
                runtime.stop()

    def test_bridge_client_page_comes_from_web(self):
        class Request:
            path = '/bridge-client.html'

        class Connection:
            def respond(self, status, body):
                self.status, self.body = status, body
                class Response:
                    headers = {'Content-Type': 'text/plain'}
                return Response()

        connection = Connection()
        response = serve_demo(connection, Request())
        self.assertEqual(connection.status.value, 200)
        self.assertIn('60710', connection.body)
        self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertIsNone(serve_demo(connection, type('R', (), {'path': '/missing.html'})()))

    def test_access_denied_is_fatal_not_occupancy(self):
        self.assertTrue(is_port_in_use(OSError(10048, 'busy')))
        self.assertTrue(is_port_in_use(OSError(errno.EADDRINUSE, 'busy')))
        self.assertFalse(is_port_in_use(OSError(10013, 'denied')))
        with tempfile.TemporaryDirectory() as folder, \
                patch('desktop_app.serve', side_effect=OSError(errno.EACCES, 'denied')) as bind:
            events = queue.Queue()
            runtime = RelayRuntime(events, Path(folder))
            runtime.start()
            runtime.thread.join(5)
            try:
                event, payload = events.get(timeout=1)
                self.assertEqual(event, 'error')
                self.assertIn('denied access', payload['message'])
                self.assertEqual(bind.call_count, 1)
            finally:
                runtime.stop()
