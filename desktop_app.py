"""Standalone Echo3D Bridge desktop application (Python/Tkinter)."""
from __future__ import annotations

import asyncio
import queue
import socket
import threading
import tkinter as tk
from contextlib import AsyncExitStack
from datetime import datetime
from tkinter import messagebox, ttk

from echo3d_bridge.server import Relay, serve, PORT, MAX_MESSAGE_BYTES

HOST = "127.0.0.1"


class RelayRuntime:
    def __init__(self, events: queue.Queue[tuple[str, dict]]):
        self.events, self.loop = events, asyncio.new_event_loop()
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
        self.relay = Relay(event_sink=self._emit)
        async with AsyncExitStack() as stack:
            options = dict(max_size=MAX_MESSAGE_BYTES, ping_interval=20, ping_timeout=30,
                           close_timeout=1, compression=None)
            self.server = await stack.enter_async_context(serve(self.relay.handle, HOST, PORT, **options))
            # Separate loopback sockets prevent localhost resolving to an unserved ::1.
            if socket.has_ipv6:
                await stack.enter_async_context(serve(self.relay.handle, "::1", PORT, **options))
            self._emit("ready", {"url": f"ws://{HOST}:{PORT}/"})
            if not self.stop_requested.is_set():
                await self.stop_event.wait()
            self.relay.clear_transfer()

    def stop(self):
        self.stop_requested.set()
        if self.stop_event is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if self.thread.is_alive():
            self.thread.join(timeout=5)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tripo Bridge · echo3D")
        self.geometry("760x560")
        self.minsize(680, 500)
        self.events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.runtime = RelayRuntime(self.events)
        self.connected = {"studio": False, "echo3d": False}
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()
        self.runtime.start()
        self.after(100, self._drain_events)

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        self.configure(background="#383838")
        style.configure(".", background="#383838", foreground="#E0E0E0", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe", bordercolor="#555555", relief="solid")
        style.configure("TLabelframe.Label", foreground="#F9CF00")
        style.configure("TButton", background="#F9CF00", foreground="#222222", padding=(12, 6))
        style.map("TButton", background=[("active", "#DDB800")])
        style.configure("Horizontal.TProgressbar", background="#F9CF00", troughcolor="#252525")
        root = ttk.Frame(self, padding=24)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Tripo Bridge", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(root, text="echo3D · 在两个前端之间传递模型", foreground="#AAAAAA").pack(anchor="w", pady=(2, 18))

        status = ttk.LabelFrame(root, text="Bridge 状态", padding=14)
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="正在启动…")
        ttk.Label(status, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.endpoint_var = tk.StringVar(value=f"Tripo Studio：ws://{HOST}:{PORT}/")
        ttk.Label(status, textvariable=self.endpoint_var).pack(anchor="w", pady=(5, 0))
        buttons = ttk.Frame(status)
        buttons.pack(anchor="w", pady=(9, 0))
        self.copy_btn = ttk.Button(buttons, text="复制 Studio 地址", command=self.copy_endpoint, state="disabled")
        self.copy_btn.pack(side="left")
        self.echo_copy_btn = ttk.Button(buttons, text="复制 echo3D 地址", command=lambda: self.copy_endpoint("/echo3d"), state="disabled")
        self.echo_copy_btn.pack(side="left", padx=10)

        peers = ttk.LabelFrame(root, text="连接状态", padding=14)
        peers.pack(fill="x", pady=14)
        self.studio_var = tk.StringVar(value="Tripo Studio：未连接")
        self.echo_var = tk.StringVar(value="echo3D 前端：未连接")
        ttk.Label(peers, textvariable=self.studio_var).pack(anchor="w")
        ttk.Label(peers, textvariable=self.echo_var).pack(anchor="w", pady=(6, 0))

        transfer = ttk.LabelFrame(root, text="传输状态", padding=14)
        transfer.pack(fill="x")
        self.transfer_var = tk.StringVar(value="暂无传输")
        ttk.Label(transfer, textvariable=self.transfer_var).pack(anchor="w")
        self.progress = ttk.Progressbar(transfer, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(9, 0))

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=10)
        log_frame.pack(fill="both", expand=True, pady=(14, 0))
        self.log = tk.Text(log_frame, height=9, state="disabled", wrap="word", relief="flat",
                           background="#2A2A2A", foreground="#CCCCCC", font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="both", expand=True)

    def _drain_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "ready":
                    self.status_var.set("运行中")
                    self.endpoint_var.set(f"Studio：{payload['url']}   Echo3D：ws://{HOST}:{PORT}/echo3d")
                    self.copy_btn.configure(state="normal")
                    self.echo_copy_btn.configure(state="normal")
                    self._log("Bridge 已启动，等待两个前端连接")
                elif event == "error":
                    self.status_var.set("启动失败")
                    self._log(f"错误：{payload['message']}")
                    messagebox.showerror("Echo3D Bridge", payload["message"])
                elif event == "connections":
                    self._set_peer("studio", payload.get("studioConnected", False))
                    self._set_peer("echo3d", payload.get("echo3dConnected", False))
                elif event == "progress":
                    percent = float(payload.get("percent", 0))
                    self.progress.configure(value=percent)
                    self.transfer_var.set(f"{payload.get('fileName', '')} · {percent:.0f}% · {payload.get('stage', '')}")
                elif event == "result":
                    self.transfer_var.set(("echo3D 已确认处理完成：" if payload.get("success") else "传输失败：") + (payload.get("message") or payload.get("fileName", "")))
                    self.progress.configure(value=100 if payload.get("success") else 0)
                    self._log(self.transfer_var.get())
                elif event == "peer_disconnected":
                    self._log(f"{payload.get('role')} 已断开")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_peer(self, role: str, connected: bool):
        if self.connected[role] != connected:
            self._log(f"{role} {'已连接' if connected else '已断开'}")
        self.connected[role] = connected
        state = "已连接" if connected else "未连接"
        if role == "studio":
            self.studio_var.set(f"Tripo Studio：{state}")
        else:
            self.echo_var.set(f"echo3D 前端：{state}")

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", datetime.now().strftime("%H:%M:%S ") + text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 200:
            self.log.delete("1.0", f"{lines - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def copy_endpoint(self, path="/"):
        self.clipboard_clear()
        self.clipboard_append(f"ws://{HOST}:{PORT}{path}")
        self._log("已复制 WebSocket 地址")

    def close(self):
        self.runtime.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
