"""Native desktop presentation. Network and filesystem work live in desktop_app."""
import tkinter as tk
import sys
from pathlib import Path

import customtkinter as ctk

from echo3d_bridge import __version__

BG = '#131517'
PANEL = '#1D2023'
RAISED = '#272B30'
TEXT = '#F2F3F4'
MUTED = '#A6ADB5'
YELLOW = '#F9CF00'
GREEN = '#7ED5AD'
RED = '#F38B88'


class BridgeWindow(ctk.CTk):
    def _label(self, parent, text='', size=13, color=TEXT, bold=False, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont('Segoe UI', size, weight='bold' if bold else 'normal'),
                            anchor='w', **kwargs)

    def _button(self, parent, text, command, primary=False, **kwargs):
        button = ctk.CTkButton(parent, text=text, command=command, height=36,
                             corner_radius=8, font=ctk.CTkFont('Segoe UI', 13),
                             fg_color=YELLOW if primary else RAISED,
                             hover_color='#E3BC00' if primary else '#353B42',
                             text_color=BG if primary else TEXT,
                             text_color_disabled='#737A82', **kwargs)
        # CTk buttons are canvas widgets; expose conventional keyboard activation.
        self._bind_keyboard(button)
        return button

    def _bind_keyboard(self, button):
        button._canvas.configure(takefocus=1, cursor='hand2')
        button._canvas.bind('<FocusIn>', lambda event: button.configure(border_width=2, border_color=TEXT))
        button._canvas.bind('<FocusOut>', lambda event: button.configure(border_width=0))
        for key in ('<Return>', '<space>'):
            button._canvas.bind(key, lambda event, b=button: b.invoke())

    def _link_button(self, parent, text, command):
        button = ctk.CTkButton(parent, text=text, command=command, height=32, width=180,
                             corner_radius=8, border_width=0,
                             font=ctk.CTkFont('Segoe UI', 16, weight='bold', underline=True),
                             fg_color='transparent', hover_color='#32383F',
                             text_color=TEXT)
        self._bind_keyboard(button)
        button._canvas.bind('<Enter>', lambda event: button.configure(text_color=YELLOW), add='+')
        button._canvas.bind('<Leave>', lambda event: button.configure(text_color=TEXT), add='+')
        return button

    def _build_ui(self):
        ctk.set_appearance_mode('dark')
        self.configure(fg_color=BG)
        self.geometry(f'820x{self._fit_height(760)}')
        self.minsize(760, min(640, self._fit_height(760)))
        icon = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets' / 'bridge.ico'
        if sys.platform == 'win32' and icon.exists():
            self.iconbitmap(str(icon))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        root = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        root.grid(row=0, column=0, sticky='nsew', padx=28, pady=(24, 18))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(root, fg_color='transparent')
        header.grid(row=0, column=0, sticky='ew', pady=(0, 24))
        header.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(header, fg_color='transparent')
        titles.grid(row=0, column=0, sticky='w')
        self._label(titles, 'Tripo echo3D Bridge', 24, bold=True).pack(side='left', anchor='s')
        self._label(titles, f'v{__version__}', 13, MUTED).pack(side='left', anchor='s', padx=(10, 0), pady=(0, 3))
        self.status_var = tk.StringVar(value='Starting…')
        self.status_badge = self._label(header, textvariable=self.status_var, color=MUTED,
                                       fg_color=RAISED, corner_radius=12, width=114, height=30)
        self.status_badge.grid(row=0, column=1, sticky='e')

        connections = ctk.CTkFrame(root, fg_color=PANEL, corner_radius=14)
        connections.grid(row=1, column=0, sticky='ew', pady=(0, 16))
        connections.grid_columnconfigure((0, 2, 4), weight=3, uniform='peers')
        connections.grid_columnconfigure((1, 3), weight=1, uniform='links')
        self.peer_labels = {}
        self.peer_links = {}
        link_commands = {'studio': self.open_studio, 'echo3d': self.open_demo}
        for col, title, role in ((0, 'Tripo Studio', 'studio'),
                                 (2, 'Local bridge', 'local'),
                                 (4, 'echo3D', 'echo3d')):
            frame = ctk.CTkFrame(connections, fg_color='transparent')
            frame.grid(row=0, column=col, sticky='ew', padx=14, pady=24)
            if role in link_commands:
                link = self._link_button(frame, title, link_commands[role])
                link.pack(anchor='center', pady=(0, 8))
                self.peer_links[role] = link
            else:
                self._label(frame, title, 16, bold=True).pack(anchor='center', pady=(0, 8))
            label = self._label(frame, '○  Not connected' if role != 'local' else '○  Starting…', 12, MUTED)
            label.pack(anchor='center')
            self.peer_labels[role] = label
        for col in (1, 3):
            # Share the nodes' row so each line stays at their vertical midpoint.
            ctk.CTkFrame(connections, width=48, height=2, corner_radius=0,
                         fg_color='#50575F').grid(row=0, column=col, sticky='ew', padx=4)

        transfer = ctk.CTkFrame(root, fg_color=PANEL, corner_radius=14)
        transfer.grid(row=2, column=0, sticky='nsew', pady=(0, 16))
        transfer.grid_columnconfigure(0, weight=1)
        transfer.grid_rowconfigure(2, weight=1)
        self._label(transfer, 'TRANSFER', 12, MUTED, True).grid(row=0, column=0, sticky='w', padx=22, pady=(18, 0))
        self.phase_label = self._label(transfer, '', 21, bold=True, wraplength=650, justify='left')
        self.phase_label.grid(row=1, column=0, sticky='w', padx=22, pady=(8, 2))
        self.transfer_var = tk.StringVar(value='')
        self.transfer_label = self._label(transfer, textvariable=self.transfer_var, color=MUTED,
                                          wraplength=650, justify='left')
        self.transfer_label.grid(row=2, column=0, sticky='nw', padx=22, pady=(0, 14))
        self.percent_label = self._label(transfer, '—', 13, MUTED)
        self.percent_label.grid(row=1, column=1, padx=(8, 22), sticky='e')
        self.progress = ctk.CTkProgressBar(transfer, height=6, corner_radius=3, fg_color=RAISED, progress_color=RAISED)
        self._set_progress(0, YELLOW)
        self.progress.grid(row=3, column=0, columnspan=2, sticky='ew', padx=22)
        self.transfer_hint = self._label(transfer, 'Models are saved locally, even while echo3D is offline.', 12, MUTED,
                                         wraplength=650, justify='left')
        self.transfer_hint.grid(row=4, column=0, columnspan=2, sticky='w', padx=22, pady=(12, 18))
        transfer.bind('<Configure>', lambda e: (
            self.phase_label.configure(wraplength=max(200, e.width / ctk.ScalingTracker.get_widget_scaling(self) - 120)),
            self.transfer_label.configure(wraplength=max(200, e.width / ctk.ScalingTracker.get_widget_scaling(self) - 100)),
            self.transfer_hint.configure(wraplength=max(200, e.width / ctk.ScalingTracker.get_widget_scaling(self) - 44))))

        storage = ctk.CTkFrame(root, fg_color=PANEL, corner_radius=14)
        storage.grid(row=3, column=0, sticky='ew', pady=(0, 16))
        storage.grid_columnconfigure(0, weight=1)
        self.storage_open = False
        self.storage_btn = ctk.CTkButton(storage, text='Local storage  ▸', command=self._toggle_storage,
                                        height=32, width=200, anchor='w', corner_radius=8,
                                        font=ctk.CTkFont('Segoe UI', 14, weight='bold'),
                                        fg_color='transparent', hover_color='#32383F', text_color=TEXT)
        self._bind_keyboard(self.storage_btn)
        self.storage_btn.grid(row=0, column=0, sticky='w', padx=12, pady=12)
        self.cache_var = tk.StringVar(value=str(self.runtime.inbox))
        self.cache_entry = ctk.CTkEntry(storage, textvariable=self.cache_var, state='readonly',
                                       fg_color=PANEL, border_width=0, text_color=MUTED,
                                       font=ctk.CTkFont('Segoe UI', 12))
        self.open_folder_btn = self._button(storage, 'Open folder', self.open_inbox, width=105)
        self.change_cache_btn = self._button(storage, 'Change…', self.choose_inbox, width=90, state='disabled')

        self.log_frame = ctk.CTkFrame(root, fg_color=PANEL, corner_radius=14)
        self.log_frame.grid(row=4, column=0, sticky='nsew')
        self.log_frame.grid_columnconfigure(0, weight=1)
        self.log_frame.grid_rowconfigure(1, weight=1)
        log_header = ctk.CTkFrame(self.log_frame, fg_color='transparent')
        log_header.grid(row=0, column=0, sticky='ew', padx=12, pady=(10, 0))
        log_header.grid_columnconfigure(0, weight=1)
        self._label(log_header, 'Logs', 14, bold=True).grid(row=0, column=0, sticky='w')
        self.clear_log_btn = self._button(log_header, 'Clear', self.clear_log, width=72)
        self.clear_log_btn.grid(row=0, column=1, sticky='e')
        self.log = ctk.CTkTextbox(self.log_frame, fg_color=PANEL, border_width=0,
                                 text_color=MUTED, font=ctk.CTkFont('Consolas', 12), wrap='word', state='disabled',
                                 scrollbar_button_color=RAISED, scrollbar_button_hover_color='#353B42')
        self.log.grid(row=1, column=0, sticky='nsew', padx=(8, 2), pady=(4, 8))

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _toggle_storage(self):
        self.storage_open = not self.storage_open
        if self.storage_open:
            self.open_folder_btn.grid(row=0, column=1, padx=(10, 8), pady=12)
            self.change_cache_btn.grid(row=0, column=2, padx=(0, 16), pady=12)
            self.cache_entry.grid(row=1, column=0, columnspan=3, sticky='ew', padx=14, pady=(0, 12))
            self.storage_btn.configure(text='Local storage  ▾')
        else:
            self.open_folder_btn.grid_remove()
            self.change_cache_btn.grid_remove()
            self.cache_entry.grid_remove()
            self.storage_btn.configure(text='Local storage  ▸')

    def _fit_height(self, desired):
        return min(desired, max(400, int(self.winfo_screenheight() / self._get_window_scaling()) - 100))

    def _set_progress(self, value, color):
        # CustomTkinter still draws a rounded cap at 0. Match the track so 0% stays empty.
        value = max(0.0, min(1.0, float(value)))
        self.progress.configure(progress_color=color if value > 0 else RAISED)
        self.progress.set(value)

    def show_service(self, ready):
        self.status_var.set('  ●  Running' if ready else '  ●  Unavailable')
        self.status_badge.configure(text_color=GREEN if ready else RED)
        self.peer_labels['local'].configure(text='●  Ready' if ready else '●  Startup failed',
                                            text_color=GREEN if ready else RED)
        if ready:
            self.phase_label.configure(text='', text_color=TEXT)
            self.transfer_var.set('')
            self.transfer_label.configure(text_color=MUTED)
            self.transfer_hint.configure(text='Models are saved locally, even while echo3D is offline.')
            self._set_progress(0, YELLOW)
            self.percent_label.configure(text='—')

    def show_port_wait(self, port, retry_interval):
        self.status_var.set('  ●  Port in use')
        self.status_badge.configure(text_color=RED)
        self.peer_labels['local'].configure(text='●  Waiting for port', text_color=RED)
        self.phase_label.configure(text='', text_color=TEXT)
        self.transfer_var.set('Another application is using this port.')
        self.transfer_label.configure(text_color=RED)
        self.transfer_hint.configure(
            text=f'Close the other Bridge instance or application. Retrying automatically every {retry_interval:g}s; no restart needed.')
        self._set_progress(0, RED)
        self.percent_label.configure(text='Waiting')

    def show_progress(self, payload):
        percent = max(0, min(100, float(payload.get('percent', 0))))
        sending = payload.get('stage') == 'Sending to echo3D'
        self.phase_label.configure(text=payload.get('fileName') or '', text_color=TEXT)
        self.transfer_var.set('Delivering to echo3D' if sending else 'Saving to your computer')
        self.transfer_label.configure(text_color=TEXT)
        self._set_progress(percent / 100, YELLOW)
        self.percent_label.configure(text=f'{percent:.0f}%')
        self.transfer_hint.configure(text='Waiting for receiver confirmation…' if sending and percent == 100
                                     else 'Keep the bridge open until the transfer finishes.')

    def show_result(self, event, payload):
        success = bool(payload.get('success'))
        title = ('Delivered to echo3D' if event == 'delivery' else 'Saved to your computer') if success else 'Transfer needs attention'
        self.phase_label.configure(text=payload.get('fileName') or '', text_color=TEXT)
        self.transfer_var.set(title)
        self.transfer_label.configure(text_color=GREEN if success else RED)
        self._set_progress(1 if success else 0, GREEN if success else RED)
        self.percent_label.configure(text='Done' if success else 'Failed')
        hint = ('Receiver confirmed delivery. Your local copy is kept.' if event == 'delivery'
                else 'Your local copy is safe. Delivery starts when echo3D is connected.')
        self.transfer_hint.configure(text=hint if success else payload.get('message', 'Check Logs for details.'))
