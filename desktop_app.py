"""Standalone Echo3D Bridge desktop application (Python/Tkinter)."""
from __future__ import annotations

import asyncio
import errno
import os
import queue
import socket
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from pathlib import Path
from contextlib import AsyncExitStack
from datetime import datetime
from tkinter import filedialog, messagebox
from desktop_ui import BridgeWindow

from echo3d_bridge.server import Relay, serve, PORT, MAX_MESSAGE_BYTES, DEFAULT_INBOX
from echo3d_bridge.settings import load_inbox, save_inbox, validate_inbox

HOST = "127.0.0.1"
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
WEB_DIR = ROOT / 'web'


def is_port_in_use(error: OSError) -> bool:
    """Only classify errors raised while binding the listener sockets."""
    return (error.errno in (errno.EADDRINUSE, 10048)
            or getattr(error, 'winerror', None) == 10048
            or '10048' in str(error)
            or 'address already in use' in str(error).lower())


def serve_demo(connection, request):
    # Static demo assets on the existing listener; WS endpoints pass through.
    pages = {'/demo': (WEB_DIR / 'echo3d-demo.html', 'text/html; charset=utf-8'),
             '/bridge-client.html': (WEB_DIR / 'bridge-client.html', 'text/html; charset=utf-8'),
             '/echo3d-bridge-client.js': (WEB_DIR / 'echo3d-bridge-client.js', 'text/javascript; charset=utf-8')}
    item = pages.get(request.path.split('?')[0])
    if item is None or not item[0].is_file():
        return None
    response = connection.respond(HTTPStatus.OK, item[0].read_text(encoding='utf-8-sig'))
    del response.headers['Content-Type']
    response.headers['Content-Type'] = item[1]
    response.headers['Cache-Control'] = 'no-store'
    return response


class RelayRuntime:
    def __init__(self, events: queue.Queue[tuple[str, dict]], inbox: Path,
                 *, port: int = PORT, retry_interval: float = 3.0):
        self.events, self.loop = events, asyncio.new_event_loop()
        self.inbox = inbox
        self.port = port
        self.retry_interval = retry_interval
        self.thread = threading.Thread(target=self._run, name="echo3d-bridge", daemon=True)
        self.stop_event: asyncio.Event | None = None
        self.server = None
        self.relay: Relay | None = None
        self.stop_requested = threading.Event()

    def start(self):
        self.thread.start()

    def _emit(self, event: str, payload: dict):
        self.events.put((event, payload))

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as exc:
            self._emit("error", {"message": str(exc)})
        finally:
            self.loop.close()

    async def _serve(self):
        self.stop_event = asyncio.Event()
        self.relay = Relay(event_sink=self._emit, inbox=self.inbox)
        announced_conflict = False
        try:
            while not self.stop_requested.is_set() and not self.stop_event.is_set():
                bound = False
                try:
                    async with AsyncExitStack() as stack:
                        options = dict(max_size=MAX_MESSAGE_BYTES, ping_interval=20, ping_timeout=30,
                                       close_timeout=1, compression=None, process_request=serve_demo,
                                       start_serving=False)
                        # Bind both before accepting clients. A failed IPv6 bind must release IPv4.
                        listeners = [await stack.enter_async_context(
                            serve(self.relay.handle, HOST, self.port, **options))]
                        if socket.has_ipv6:
                            listeners.append(await stack.enter_async_context(
                                serve(self.relay.handle, '::1', self.port, **options)))
                        bound = True
                        if self.stop_requested.is_set() or self.stop_event.is_set():
                            break
                        for listener in listeners:
                            await listener.start_serving()
                        self.server = listeners[0]
                        self._emit('ready', {'url': f'ws://{HOST}:{self.port}/'})
                        await self.stop_event.wait()
                    break
                except OSError as exc:
                    if bound or not is_port_in_use(exc):
                        if not bound and (exc.errno in (errno.EACCES, 10013)
                                          or getattr(exc, 'winerror', None) == 10013
                                          or '10013' in str(exc)):
                            raise RuntimeError(
                                f'The system denied access to port {self.port}. '
                                'Check reserved ports or permissions, then restart Bridge.') from exc
                        raise
                    if not announced_conflict:
                        self._emit('port_in_use', {'port': self.port, 'retryInterval': self.retry_interval})
                        announced_conflict = True
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=self.retry_interval)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.server = None
            self.relay.clear_transfer()

    def change_inbox(self, path: Path):
        async def apply():
            try:
                self.inbox = await self.relay.change_inbox(path)
                self._emit('cache_changed', {'path': str(self.inbox)})
            except Exception as exc:
                self._emit('cache_error', {'message': str(exc)})
        if self.loop.is_running() and self.relay is not None:
            asyncio.run_coroutine_threadsafe(apply(), self.loop)
        else:
            # Permit correcting an unavailable startup directory without losing the UI.
            def save_for_restart():
                try:
                    candidate = validate_inbox(path)
                    save_inbox(candidate)
                    self.inbox = candidate
                    self._emit('cache_changed', {'path': str(candidate), 'restart': True})
                except Exception as exc:
                    self._emit('cache_error', {'message': str(exc)})
            threading.Thread(target=save_for_restart, daemon=True).start()

    def stop(self):
        self.stop_requested.set()
        if self.stop_event is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if self.thread.is_alive():
            self.thread.join(timeout=5)


class App(BridgeWindow):
    def __init__(self):
        super().__init__()
        self.title("Tripo echo3D Bridge")
        self.events: queue.Queue[tuple[str, dict]] = queue.Queue()
        settings_error = None
        try:
            inbox = load_inbox()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            inbox = DEFAULT_INBOX.resolve()
            settings_error = f'Unable to read cache settings. Using the default folder: {exc}'
        self.runtime = RelayRuntime(self.events, inbox)
        self.connected = {"studio": False, "echo3d": False}
        self.demo_ready = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()
        self._log(f'Local connection · port {PORT}')
        if settings_error:
            self._log(settings_error)
        self.runtime.start()
        self.after(100, self._drain_events)

    def open_studio(self):
        if not self.demo_ready:
            self._log('Tripo Studio client is available once the bridge is running.')
            return
        webbrowser.open(f'http://{HOST}:{PORT}/bridge-client.html')

    def open_demo(self):
        if not self.demo_ready:
            self._log('echo3D demo page is available once the bridge is running.')
            return
        webbrowser.open(f'http://{HOST}:{PORT}/demo')

    def _drain_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "ready":
                    self.show_service(True)
                    self.demo_ready = True
                    self.change_cache_btn.configure(state='normal')
                    self._log("Bridge started.")
                elif event == 'port_in_use':
                    self.demo_ready = False
                    self.change_cache_btn.configure(state='normal')
                    self.show_port_wait(payload['port'], payload['retryInterval'])
                    self._log(f"Port {payload['port']} is in use. Retrying every {payload['retryInterval']:g}s.")
                elif event == "error":
                    self.change_cache_btn.configure(state='normal')
                    self.demo_ready = False
                    self.show_service(False)
                    self.phase_label.configure(text='', text_color='#F2F3F4')
                    self.transfer_var.set(payload['message'] or 'Bridge could not start')
                    self.transfer_label.configure(text_color='#F38B88')
                    self.transfer_hint.configure(text='Check Logs for details. Correct the problem, then restart Bridge.')
                    self._log(f"Error: {payload['message']}")
                    messagebox.showerror("Echo3D Bridge", payload["message"])
                elif event == 'cache_changed':
                    self.cache_var.set(payload['path'])
                    self.change_cache_btn.configure(state='normal')
                    self._log('Cache folder updated: ' + payload['path'])
                    if payload.get('restart'):
                        self.status_var.set('  Restart required')
                        self.transfer_hint.configure(text='Cache settings updated. Restart Bridge to apply.')
                elif event == 'cache_error':
                    self.change_cache_btn.configure(state='normal')
                    self._log('Cache folder unchanged: ' + payload['message'])
                    messagebox.showerror('Unable to Change Cache Folder', payload['message'], parent=self)
                elif event == "connections":
                    self._set_peer("studio", payload.get("studioConnected", False))
                    self._set_peer("echo3d", payload.get("echo3dConnected", False))
                elif event == "progress":
                    self.show_progress(payload)
                elif event in ("result", "delivery"):
                    self.show_result(event, payload)
                    self._log(payload.get('message') or payload.get('fileName', ''))
                    if payload.get('localPath'): self._log('Local file: ' + payload['localPath'])
                elif event == "peer_disconnected":
                    self._log(f"{payload.get('role')} disconnected")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_peer(self, role: str, connected: bool):
        if self.connected[role] != connected:
            self._log(f"{role} {'Connected' if connected else 'disconnected'}")
        self.connected[role] = connected
        self.peer_labels[role].configure(text='●  Connected' if connected else '○  Not connected',
                                         text_color='#7ED5AD' if connected else '#A6ADB5')

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", datetime.now().strftime("%H:%M:%S ") + text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 200:
            self.log.delete("1.0", f"{lines - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        self.runtime.stop()
        self.destroy()

    def open_inbox(self):
        try:
            self.runtime.inbox.mkdir(parents=True, exist_ok=True)
            folder = str(self.runtime.inbox)
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder], check=True)
            else:
                subprocess.run(['xdg-open', folder], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            messagebox.showerror('Unable to Open Cache Folder', str(exc), parent=self)

    def choose_inbox(self):
        selected = filedialog.askdirectory(parent=self, title='Select Cache Folder',
                                          initialdir=str(self.runtime.inbox), mustexist=False)
        if selected:
            self.change_cache_btn.configure(state='disabled')
            self.runtime.change_inbox(Path(selected))


if __name__ == "__main__":
    App().mainloop()
