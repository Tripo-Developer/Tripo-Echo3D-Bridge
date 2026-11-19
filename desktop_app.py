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
from desktop_ui import GREEN, MUTED, RED, TEXT, BridgeWindow

from cloud_receiver import Ledger, Receiver, collection_scope
from echo3d_bridge import __version__
from echo3d_bridge.auto_update import Updater, packaged_executable, update_display
from echo3d_bridge.server import Relay, serve, PORT, MAX_MESSAGE_BYTES, DEFAULT_INBOX
from echo3d_bridge.settings import APP_DATA, clear_cached_transfers, load_inbox, save_inbox, validate_inbox

HOST = "127.0.0.1"
STUDIO_URL = "https://studio.tripo3d.ai/workspace/generate"
ECHO3D_URL = "https://www.echo3d.com"
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
        self._cloud_lock = threading.Lock()
        self._cloud_task: asyncio.Task | None = None
        self._cloud_starting = False
        self._cloud_stop = False

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

    def clear_cache(self):
        """Remove finished transfer folders. An in-progress transfer is kept."""
        if self.loop.is_running() and self.relay is not None:
            asyncio.run_coroutine_threadsafe(self._clear_cache(), self.loop)
            return
        self._emit('cache_cleared', {'removed': clear_cached_transfers(self.inbox)})

    async def _clear_cache(self):
        protected = []
        if self.relay.transfer is not None:
            protected.append(self.relay.transfer.folder)
        if self.relay.delivery_lock.locked():
            protected.extend(folder for folder, _model, _meta in await asyncio.to_thread(self.relay.pending))
        removed = await asyncio.to_thread(clear_cached_transfers, self.inbox, protected)
        self._emit('cache_cleared', {'removed': removed})

    def cloud_active(self):
        with self._cloud_lock:
            return self._cloud_starting or (self._cloud_task is not None and not self._cloud_task.done())

    def start_cloud(self, credentials, *, ledger_path=None, uploader=None):
        """Connect the upload receiver to this bridge. Credentials stay in memory only."""
        if not self.loop.is_running():
            self._emit('cloud_error', {'message': 'Bridge is not running.'})
            return False
        with self._cloud_lock:
            if self._cloud_starting or (self._cloud_task is not None and not self._cloud_task.done()):
                return False
            self._cloud_stop = False
            self._cloud_starting = True
        try:
            asyncio.run_coroutine_threadsafe(
                self._launch_cloud(credentials, ledger_path, uploader), self.loop)
        except Exception:
            with self._cloud_lock:
                self._cloud_starting = False
            raise
        return True

    async def _launch_cloud(self, credentials, ledger_path, uploader):
        with self._cloud_lock:
            stopped = self._cloud_stop
        if stopped:
            with self._cloud_lock:
                self._cloud_starting = False
            return
        ledger = Ledger(ledger_path or (APP_DATA / 'cloud-receipts.sqlite3'))
        try:
            scope = collection_scope(credentials['key'])
            self._emit('cloud_log', {'message': f'Connecting echo3D receiver. Collection fingerprint {scope}.'})
            kwargs = {'log': lambda text: self._emit('cloud_log', {'message': text})}
            if uploader is not None:
                kwargs['uploader'] = uploader
            receiver = Receiver(ledger, scope, credentials, **kwargs)
            task = asyncio.create_task(receiver.run(f'ws://127.0.0.1:{self.port}/echo3d'))
        except Exception as exc:
            with self._cloud_lock:
                self._cloud_starting = False
            ledger.close()
            self._emit('cloud_error', {'message': str(exc)})
            return
        with self._cloud_lock:
            self._cloud_task = task
            self._cloud_starting = False
            stopped = self._cloud_stop
        task.add_done_callback(lambda done, held=ledger: self._finish_cloud(done, held))
        if stopped and not task.done():
            task.cancel()

    def _finish_cloud(self, task, ledger):
        try:
            task.result()
        except asyncio.CancelledError:
            self._emit('cloud_log', {'message': 'echo3D receiver stopped.'})
        except Exception as exc:
            self._emit('cloud_error', {'message': str(exc).strip() or 'echo3D receiver stopped.'})
        else:
            self._emit('cloud_log', {'message': 'echo3D receiver stopped.'})
        finally:
            with self._cloud_lock:
                if self._cloud_task is task:
                    self._cloud_task = None
                self._cloud_starting = False
            ledger.close()

    async def _stop_cloud(self):
        with self._cloud_lock:
            task = self._cloud_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def stop_cloud(self, wait=False, timeout=240):
        with self._cloud_lock:
            self._cloud_stop = True
            active = self._cloud_starting or (self._cloud_task is not None and not self._cloud_task.done())
        if not active or not self.loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_cloud(), self.loop)
        if not wait:
            return
        try:
            future.result(timeout)
        except Exception as exc:
            self._emit('cloud_log', {'message': f'echo3D receiver stop did not finish: {exc}'})

    def stop(self):
        if self.loop.is_running():
            self.stop_cloud(wait=True)
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
        self._restarting = False
        self._update_checked = False
        self._update_seen = None
        self.updater = Updater(__version__, APP_DATA / 'updates',
                               is_busy=self._update_busy, on_restart=self._restart_for_update)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()
        self.show_update(self._update_display())
        self._log(f'Server running on port: {PORT}')
        if settings_error:
            self._log(settings_error)
        self.runtime.start()
        self.after(100, self._drain_events)

    def open_studio(self):
        webbrowser.open(STUDIO_URL)

    def open_echo3d_site(self):
        webbrowser.open(ECHO3D_URL)

    def open_echo3d(self):
        if not self.demo_ready:
            self._log('echo3D upload is available once the bridge is running.')
            return
        if self.runtime.cloud_active():
            if messagebox.askyesno(
                    'Disconnect echo3D',
                    'Stop the echo3D upload receiver?\n\nModels already accepted stay in the collection. '
                    'If an upload was in progress, check the collection before connecting again.',
                    parent=self):
                self.runtime.stop_cloud()
            return
        credentials = self.ask_cloud_credentials()
        if not credentials:
            return
        if not self.runtime.start_cloud(credentials):
            self._log('echo3D receiver is already running.')

    def _drain_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "ready":
                    self.show_service(True)
                    self.demo_ready = True
                    self.change_cache_btn.configure(state='normal')
                    self._log("Bridge started.")
                    if not self._update_checked:
                        self._update_checked = True
                        self.updater.start_check(manual=False)
                elif event == 'port_in_use':
                    self.demo_ready = False
                    self.change_cache_btn.configure(state='normal')
                    self.show_port_wait(payload['port'], payload['retryInterval'])
                    self._log(f"Port {payload['port']} is in use. Retrying every {payload['retryInterval']:g}s.")
                elif event == "error":
                    self.change_cache_btn.configure(state='normal')
                    self.demo_ready = False
                    self.show_service(False)
                    self.phase_label.configure(text='', text_color=TEXT)
                    self.transfer_var.set(payload['message'] or 'Bridge could not start')
                    self.transfer_label.configure(text_color=RED)
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
                elif event == 'cache_cleared':
                    self._log(f"Cache clear removed {payload.get('removed', 0)} folder(s).")
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
                elif event == 'cloud_log':
                    if payload.get('message'):
                        self._log(payload['message'])
                elif event == 'cloud_error':
                    message = payload.get('message') or 'echo3D receiver stopped.'
                    self._log('echo3D: ' + message)
                    messagebox.showerror('echo3D', message, parent=self)
        except queue.Empty:
            pass
        self._poll_update()
        if self._restarting:
            self.close()
            return
        self.after(100, self._drain_events)

    def _update_block_reason(self):
        if self._transfer_active:
            return 'transfer'
        if self.connected.get('studio') or self.connected.get('echo3d') or self.runtime.cloud_active():
            return 'Disconnect Tripo Studio and echo3D before updating'
        return ''

    def _update_busy(self):
        return bool(self._update_block_reason())

    def _update_display(self):
        reason = self._update_block_reason()
        return update_display(status=self.updater.status, state=self.updater.state, notice=self.updater.notice,
                              update_ready=self.updater.update_ready,
                              transferring=reason == 'transfer',
                              blocked='' if reason in ('', 'transfer') else reason,
                              can_install=packaged_executable() is not None)

    def _update_snapshot(self):
        return (self.updater.status.get('checking'), self.updater.status.get('disabled'),
                self.updater.state.get('phase'), self.updater.notice.get('available'),
                self.updater.notice.get('latest'), self.updater.notice.get('error'),
                self.updater.state.get('error'), self.updater.status.get('checked'))

    def _poll_update(self):
        before = self._update_seen
        self.updater.poll()
        self.show_update(self._update_display())
        after = self._update_snapshot()
        if after != before:
            self._log_update_change(after)
            self._update_seen = after

    def _log_update_change(self, snapshot):
        checking, disabled, phase, available, latest, error, install_error, checked = snapshot
        if phase == 'failed' and install_error:
            self._log('Update failed: ' + install_error)
        elif phase == 'installed':
            self._log('Installed %s. Restarting.' % (latest or ''))
        elif phase == 'downloading':
            self._log('Downloading update.')
        elif error:
            self._log('Could not check for updates: ' + error)
        elif available:
            self._log('New version %s is available.' % latest)
        elif disabled or not checked or checking:
            return
        else:
            self._log('Echo3D Bridge is on the latest version.')

    def _on_update(self):
        """First click checks. A second click installs that release on the packaged app."""
        if self._update_display().busy:
            return
        if self.updater.update_ready:
            if packaged_executable() is None:
                self._log('This window is running from source. The packaged Windows app installs the update and restarts.')
                return
            if self.updater.notice.get('auto_install'):
                self.updater.start_install()
            else:
                self._log('This release requires a manual download.')
        else:
            self.updater.request_check()
        self.show_update(self._update_display())

    def _restart_for_update(self):
        self._restarting = True

    def _set_peer(self, role: str, connected: bool):
        if self.connected[role] != connected:
            self._log(f"{role} {'Connected' if connected else 'disconnected'}")
        self.connected[role] = connected
        text = 'Connected' if connected else 'Not connected'
        self.peer_labels[role].configure(text=text, text_color=GREEN if connected else MUTED)
        if role == 'echo3d':
            self.note_echo3d(connected)
        self._apply_connection_headline()

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", datetime.now().strftime("[%H:%M:%S] ") + text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 200:
            self.log.delete("1.0", f"{lines - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        if not self._restarting:
            self.updater.shutdown()
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

    def clear_cache(self):
        if not messagebox.askyesno(
                'Clear cache',
                'Clear cached transfers?\n\nFolders still used by a transfer in progress are kept. '
                'The cache directory itself stays.',
                parent=self):
            return
        self.runtime.clear_cache()


if __name__ == "__main__":
    App().mainloop()
