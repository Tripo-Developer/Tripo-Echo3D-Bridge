"""Native desktop presentation. Network and filesystem work live in desktop_app.

One Blender-style panel: the same tokens, artwork, and icons as the Blender and
Substance Painter bridges. The Studio → bridge → echo3D diagram stays.
"""
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

from echo3d_bridge import __version__
from echo3d_bridge.server import PORT

# Tokens shared with Blender ui/panel_painter.py and Substance Painter panel_style.py.
YELLOW = '#FFC700'
TEXT = '#d4d4d8'
MUTED = '#a1a1aa'
DIM = '#71717a'
GREEN = '#34d399'
RED = '#fb7185'
SEPARATOR = '#48484e'
BG = '#363639'
PROGRESS_TRACK = '#202023'
LOG_BACKGROUND = '#161618'
FIELD_BACKGROUND = '#1c1c1f'
FIELD_BORDER = '#38383e'

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
ASSET_DIR = ROOT / 'assets' / 'bridge_ui'


class BridgeWindow(ctk.CTk):
    def _image(self, filename, size):
        # Tkinter only keeps alpha for RGBA. LA loses it on resize, so
        # transparent pixels become a black square and the strokes blur.
        image = Image.open(ASSET_DIR / filename).convert('RGBA')
        photo = ctk.CTkImage(light_image=image, dark_image=image, size=size)
        self._images.append(photo)
        return photo

    def _label(self, parent, text='', size=13, color=TEXT, bold=False, **kwargs):
        anchor = kwargs.pop('anchor', 'w')
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont('Segoe UI', size, weight='bold' if bold else 'normal'),
                            anchor=anchor, **kwargs)

    def _button(self, parent, text, command, primary=False, image=None, **kwargs):
        button = ctk.CTkButton(parent, text=text, command=command, height=32,
                             image=image, compound='left',
                             corner_radius=6, font=ctk.CTkFont('Segoe UI', 13),
                             fg_color=YELLOW if primary else FIELD_BACKGROUND,
                             hover_color='#E3B400' if primary else '#2a2a2e',
                             border_width=0 if primary else 1,
                             border_color=FIELD_BORDER,
                             text_color=BG if primary else TEXT,
                             text_color_disabled=DIM, **kwargs)
        self._bind_keyboard(button)
        return button

    def _bind_keyboard(self, button):
        button._canvas.configure(takefocus=1, cursor='hand2')
        button._canvas.bind('<FocusIn>', lambda event: button.configure(border_width=2, border_color=TEXT))
        button._canvas.bind('<FocusOut>', lambda event: button.configure(border_width=0 if button.cget('fg_color') == YELLOW else 1))
        for key in ('<Return>', '<space>'):
            button._canvas.bind(key, lambda event, b=button: b.invoke())

    def _link_button(self, parent, text, command):
        button = ctk.CTkButton(parent, text=text, command=command, height=24, width=86,
                             corner_radius=6, border_width=0,
                             font=ctk.CTkFont('Segoe UI', 12, weight='bold'),
                             fg_color='transparent', hover_color='#2f2f33',
                             text_color=TEXT)
        self._bind_keyboard(button)
        button._canvas.bind('<Enter>', lambda event: button.configure(text_color=YELLOW), add='+')
        button._canvas.bind('<Leave>', lambda event: button.configure(text_color=TEXT), add='+')
        return button

    def _icon_button(self, parent, image, command):
        """Square action, the same 28px control the reference panel uses."""
        button = ctk.CTkButton(parent, text='', command=command, width=28, height=28, image=image,
                             corner_radius=6, fg_color='#27272b', hover_color='#34343a',
                             border_width=1, border_color='#3e3e44', text_color=TEXT)
        self._bind_keyboard(button)
        return button

    def _separator(self, parent):
        return ctk.CTkFrame(parent, height=1, corner_radius=0, fg_color=SEPARATOR)

    def _section(self, parent, row, padx=16, pady=(8, 0)):
        frame = ctk.CTkFrame(parent, fg_color='transparent')
        # padx must go through CTk grid so it scales with the footer padding.
        frame.grid(row=row, column=0, sticky='ew', padx=padx, pady=pady)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkFrame(frame, height=1, corner_radius=0, fg_color='#3c3c42').grid(
            row=0, column=0, sticky='ew', pady=(0, 8))
        return frame

    def _build_ui(self):
        ctk.set_appearance_mode('dark')
        self._images = []
        self.configure(fg_color=BG)
        # Short minimum so the panel can be resized down. Width stays a narrow column.
        self.minsize(380, 320)
        self.maxsize(480, 1600)
        icon = ROOT / 'assets' / 'bridge.ico'
        if sys.platform == 'win32' and icon.exists():
            self.iconbitmap(str(icon))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        root = self._mount_scroll()

        header = ctk.CTkFrame(root, fg_color='transparent')
        header.grid(row=0, column=0, sticky='ew', padx=16, pady=(22, 16))
        logo = ctk.CTkFrame(header, fg_color='transparent')
        logo.pack()
        ctk.CTkLabel(logo, text='', image=self._image('brand-symbol.png', (28, 28))).pack(side='left')
        ctk.CTkLabel(logo, text='', image=self._image('brand-wordmark.png', (88, 16))).pack(side='left', padx=(8, 0))

        connection = self._section(root, 1, pady=(2, 0))
        self._port_blocked = self._listener_failed = self._listener_ready = False
        self.status_var = tk.StringVar(value='Waiting for connection')
        self.status_badge = self._label(connection, textvariable=self.status_var, color=YELLOW, size=13)
        self._globe_icon = self._image('icon-globe-check.png', (14, 14))

        connections = ctk.CTkFrame(connection, fg_color='transparent')
        connections.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        connections.grid_columnconfigure((0, 2, 4), weight=0)
        connections.grid_columnconfigure((1, 3), weight=1, minsize=20)
        self.peer_labels = {}
        self.peer_links = {}
        self._node_icons = []
        link_commands = {'studio': self.open_studio, 'echo3d': self.open_echo3d_site}
        nodes = ((0, 'Tripo Studio', 'studio', 'brand-symbol.png'),
                 (2, 'Local bridge', 'local', 'icon-server.png'),
                 (4, 'echo3D', 'echo3d', 'icon-echo3d.png'))
        icon_sizes = {'brand-symbol.png': (22, 22), 'icon-echo3d.png': (22, 22), 'icon-server.png': (22, 22)}
        for col, title, role, icon_name in nodes:
            size = icon_sizes.get(icon_name, (14, 14))
            icon = ctk.CTkLabel(connections, text='', image=self._image(icon_name, size))
            icon.grid(row=0, column=col)
            self._node_icons.append(icon)
            if role in link_commands:
                link = self._link_button(connections, title, link_commands[role])
                link.grid(row=1, column=col, pady=(2, 0))
                self.peer_links[role] = link
            else:
                self._label(connections, title, 12, bold=True).grid(row=1, column=col, pady=(2, 0))
            if role == 'echo3d':
                status = ctk.CTkFrame(connections, fg_color='transparent')
                status.grid(row=2, column=col)
                self._echo_login_icon = self._image('icon-log-in.png', (14, 14))
                self._echo_connected = False
                self._echo_status_icon = ctk.CTkLabel(status, text='', image=self._echo_login_icon, height=16)
                self._echo_status_icon.pack(side='left')
                label = self._label(status, 'Not connected', 11, RED, height=16)
                label.pack(side='left', padx=(4, 0))
                self._bind_echo_status(status, label)
                self.connect_btn = label
            elif role == 'studio':
                status = ctk.CTkFrame(connections, fg_color='transparent')
                status.grid(row=2, column=col)
                ctk.CTkLabel(status, text='', image=self._globe_icon, height=16).pack(side='left')
                label = self._label(status, 'Waiting for connection', 11, YELLOW, height=16)
                label.pack(side='left', padx=(4, 0))
            else:
                label = None
            if label is not None:
                self.peer_labels[role] = label
        self._link_lines = [tk.Frame(connections, height=2, bg='#5c5c64', bd=0, highlightthickness=0) for _ in (0, 1)]
        self._link_line_job = None
        self._placing_links = False
        connections.bind('<Configure>', self._schedule_link_lines)

        transfer = ctk.CTkFrame(connection, fg_color='transparent')
        transfer.grid(row=4, column=0, sticky='ew', pady=(12, 4))
        transfer.grid_columnconfigure(0, weight=1)
        self._label(transfer, 'Transfer Progress', 12, MUTED).grid(row=0, column=0, sticky='w')
        self.phase_label = self._label(transfer, '', 13, bold=True, wraplength=320, justify='left')
        self.phase_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.transfer_var = tk.StringVar(value='')
        self.transfer_label = self._label(transfer, textvariable=self.transfer_var, color=MUTED, size=12,
                                          wraplength=320, justify='left')
        self.transfer_label.grid(row=2, column=0, columnspan=2, sticky='w', pady=(2, 4))
        sides = ctk.CTkFrame(transfer, fg_color='transparent')
        sides.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(4, 0))
        sides.grid_columnconfigure((0, 1), weight=1)
        self.progress_track = tk.Canvas(sides, height=8, bg=PROGRESS_TRACK, bd=0, highlightthickness=1,
                                        highlightbackground='#2b2b2f')
        self.progress_track.grid(row=0, column=0, columnspan=2, sticky='ew')
        self.progress_track.bind('<Configure>', lambda _event: self._draw_progress())
        self._local_progress = 0.0
        self._echo_progress = 0.0
        self._transfer_active = False
        self.transfer_hint = self._label(sides, 'Models are saved locally, even while echo3D is offline.',
                                         11, MUTED, wraplength=320, justify='left')
        self.transfer_hint.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        self._sync_transfer_rows()

        storage = self._section(root, 2)
        self.storage_open = False
        self._settings_closed = self._image('icon-chevron-right.png', (16, 16))
        self._settings_open = self._image('icon-chevron-down.png', (16, 16))
        settings_head = ctk.CTkFrame(storage, fg_color='transparent')
        settings_head.grid(row=1, column=0, sticky='ew')
        ctk.CTkLabel(settings_head, text='', image=self._image('icon-settings.png', (16, 16))).pack(side='left')
        self.storage_btn = ctk.CTkButton(settings_head, text='Settings', image=self._settings_closed, compound='right',
                                        command=self._toggle_storage, height=28, anchor='w', corner_radius=6,
                                        font=ctk.CTkFont('Segoe UI', 13, weight='bold'),
                                        fg_color='transparent', hover_color='#2f2f33', text_color=TEXT)
        self._bind_keyboard(self.storage_btn)
        self.storage_btn.pack(side='left', padx=(6, 0))
        self._settings_body = ctk.CTkFrame(storage, fg_color='transparent')
        self._settings_body.grid_columnconfigure(0, weight=1)
        echo_head = ctk.CTkFrame(self._settings_body, fg_color='transparent')
        echo_head.grid(row=0, column=0, sticky='ew', pady=(2, 0))
        ctk.CTkLabel(echo_head, text='', image=self._image('icon-echo3d.png', (16, 16)), height=18).pack(side='left')
        self._label(echo_head, 'echo3D', 12, bold=True, height=18).pack(side='left', padx=(6, 0))
        self.credential_entries = {}
        self._key_attention = False
        fields = (('key', 'Collection key', True, False),
                  ('email', 'Account email', False, False),
                  ('userKey', 'User Authentication Key', True, False),
                  ('secKey', 'Security key', True, True))
        for index, (name, label, secret, optional) in enumerate(fields):
            row = 2 + index * 2
            caption = ctk.CTkFrame(self._settings_body, fg_color='transparent')
            caption.grid(row=row, column=0, sticky='ew', pady=(4, 0))
            text = f'{label} (Optional)' if optional else label
            self._label(caption, text, 11, MUTED, height=16).grid(row=0, column=0, sticky='w')
            options = {'show': '*'} if secret else {}
            entry = ctk.CTkEntry(self._settings_body, height=24, border_width=2, fg_color=FIELD_BACKGROUND,
                                 border_color=FIELD_BORDER, text_color=TEXT, font=ctk.CTkFont('Consolas', 11), **options)
            entry.grid(row=row + 1, column=0, sticky='ew', pady=(1, 0))
            if name != 'secKey':
                entry.bind('<KeyRelease>', lambda _event, widget=entry: self._restyle_key_entry(widget))
            self.credential_entries[name] = entry
        ctk.CTkFrame(self._settings_body, height=1, corner_radius=0, fg_color='#3c3c42').grid(
            row=11, column=0, sticky='ew', pady=(4, 0))
        self._cache_head = ctk.CTkFrame(self._settings_body, fg_color='transparent')
        self._cache_head.grid(row=12, column=0, sticky='ew', pady=(8, 0))
        self._cache_head.grid_columnconfigure(0, weight=1)
        self._label(self._cache_head, 'Cache Directory', 11, MUTED).grid(row=0, column=0, sticky='w')
        self._cache_actions = ctk.CTkFrame(self._cache_head, fg_color='transparent')
        self._cache_actions.grid(row=0, column=1, sticky='e')
        self.change_cache_btn = self._icon_button(self._cache_actions, self._image('icon-square-pen.png', (14, 14)),
                                                  self.choose_inbox)
        self.change_cache_btn.configure(state='disabled')
        self.change_cache_btn.pack(side='left')
        self.clear_cache_btn = self._icon_button(self._cache_actions, self._image('icon-clean.png', (14, 14)),
                                                 self.clear_cache)
        self.clear_cache_btn.pack(side='left', padx=(6, 0))
        self.cache_var = tk.StringVar(value=str(self.runtime.inbox))
        path_row = ctk.CTkFrame(self._cache_head, fg_color='transparent')
        path_row.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        path_row.grid_columnconfigure(0, weight=1)
        self.cache_entry = ctk.CTkEntry(path_row, textvariable=self.cache_var, state='readonly', height=28,
                                       fg_color=FIELD_BACKGROUND, border_color=FIELD_BORDER, text_color=MUTED,
                                       font=ctk.CTkFont('Consolas', 11))
        self.open_folder_btn = self._icon_button(path_row, self._image('icon-folder.png', (14, 14)), self.open_inbox)
        self.open_folder_btn.grid(row=0, column=1, padx=(6, 0))

        log_section = self._section(root, 3, pady=(8, 4))
        self.log_frame = log_section
        self.log_open = True
        log_header = ctk.CTkFrame(log_section, fg_color='transparent')
        log_header.grid(row=1, column=0, sticky='ew')
        log_header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(log_header, text='', image=self._image('icon-terminal.png', (16, 16))).grid(row=0, column=0)
        self.log_btn = ctk.CTkButton(log_header, text='Log', image=self._settings_open, compound='right',
                                    command=self._toggle_log, height=28, anchor='w', corner_radius=6,
                                    font=ctk.CTkFont('Segoe UI', 13, weight='bold'),
                                    fg_color='transparent', hover_color='#2f2f33', text_color=TEXT)
        self._bind_keyboard(self.log_btn)
        self.log_btn.grid(row=0, column=1, sticky='w', padx=(6, 0))
        self.clear_log_btn = self._icon_button(log_header, self._image('icon-trash.png', (14, 14)), self.clear_log)
        self.clear_log_btn.grid(row=0, column=2, sticky='e')
        self.log = ctk.CTkTextbox(log_section, height=112, fg_color=LOG_BACKGROUND, border_width=1,
                                 border_color='#27272b', corner_radius=6,
                                 text_color=TEXT, font=ctk.CTkFont('Consolas', 11), wrap='word', state='disabled',
                                 scrollbar_button_color=FIELD_BORDER, scrollbar_button_hover_color='#2a2a2e')
        self.log.grid(row=2, column=0, sticky='ew', pady=(8, 0))

        footer = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self._footer = footer
        self._footer_pad = 16
        footer.grid(row=1, column=0, sticky='sew', padx=16, pady=(0, 10))
        ctk.CTkFrame(footer, height=1, corner_radius=0, fg_color='#3c3c42').pack(fill='x', pady=(0, 8))
        foot = ctk.CTkFrame(footer, fg_color='transparent', height=28)
        foot.pack(fill='x')
        foot.grid_columnconfigure(2, weight=1)
        self._label(foot, 'Version', 11, DIM).grid(row=0, column=0, sticky='w')
        self._label(foot, __version__, 11, TEXT).grid(row=0, column=1, sticky='w', padx=(8, 0))
        self.version_hint = self._label(foot, '', 11, MUTED)
        self.version_hint.grid(row=0, column=2, sticky='e', padx=(8, 8))
        self._refresh_icon = self._image('icon-refresh.png', (14, 14))
        self._download_icon = self._image('icon-download.png', (14, 14))
        self.update_btn = self._icon_button(foot, self._refresh_icon, self._on_update)
        self.update_btn.grid(row=0, column=3, sticky='e')
        self._update_signature = None
        self._fit_opening_window()
        self.after_idle(self._sync_scroll)

    def _mount_scroll(self):
        """Equal left and right gutters. The thumb uses only the right gutter."""
        body = tk.Frame(self, bg=BG, bd=0, highlightthickness=0)
        body.grid(row=0, column=0, sticky='nsew')
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._scroll_body = body
        self._left_gutter = tk.Frame(body, width=8, bg=BG, bd=0, highlightthickness=0)
        self._left_gutter.grid(row=0, column=0, sticky='ns')
        self._left_gutter.grid_propagate(False)
        canvas = tk.Canvas(body, bg=BG, bd=0, highlightthickness=0)
        canvas.grid(row=0, column=1, sticky='nsew')
        self._canvas = canvas
        self._right_gutter = tk.Frame(body, width=8, bg=BG, bd=0, highlightthickness=0)
        self._right_gutter.grid(row=0, column=2, sticky='ns')
        self._right_gutter.grid_propagate(False)
        self._scrollbar = ctk.CTkScrollbar(self._right_gutter, orientation='vertical', command=canvas.yview,
                                           width=8, fg_color=BG, button_color=BG, button_hover_color=BG,
                                           corner_radius=4, border_spacing=0)
        self._scrollbar.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=self._on_scroll)
        content = ctk.CTkFrame(canvas, fg_color=BG, corner_radius=0, border_width=0)
        content.grid_columnconfigure(0, weight=1)
        self._content = content
        self._window_id = canvas.create_window((0, 0), window=content, anchor='nw')
        self._scroll_signature = None
        self._syncing_scroll = False
        self._scroll_dirty = False
        canvas.bind('<Configure>', self._sync_scroll)
        content.bind('<Configure>', self._sync_scroll)
        self.bind('<Configure>', self._sync_scroll, add='+')
        self.bind_all('<MouseWheel>', self._on_mousewheel, add='+')
        self.bind_all('<Button-4>', self._on_mousewheel, add='+')
        self.bind_all('<Button-5>', self._on_mousewheel, add='+')
        return content

    def _on_scroll(self, first, last):
        self._scrollbar.set(first, last)

    def _sync_scroll(self, event=None):
        # Moving the window also sends Configure. Only a size change needs a new scroll region.
        if event is not None and getattr(event, 'widget', None) is self:
            size = (event.width, event.height)
            if size == getattr(self, '_configured_size', None):
                return
            self._configured_size = size
        if self._syncing_scroll:
            # A resize arrived while we were measuring. Apply the settled size once more.
            self._scroll_dirty = True
            return
        canvas = self._canvas
        self._syncing_scroll = True
        try:
            for _ in range(4):
                self._scroll_dirty = False
                try:
                    if not canvas.winfo_exists() or not self.update_btn.winfo_exists():
                        return
                    self._content.update_idletasks()
                    width = canvas.winfo_width()
                    view_height = canvas.winfo_height()
                    scale = float(self.update_btn._get_widget_scaling() or 1)
                except (tk.TclError, AttributeError):
                    return
                if width <= 1 or view_height <= 1 or not hasattr(self, '_footer'):
                    return
                content_height = max(self._content.winfo_reqheight(), 1)
                gutter = max(self._scrollbar.winfo_reqwidth(), 8)
                overflow = content_height > view_height + 1
                signature = (width, view_height, content_height, gutter, overflow)
                if signature == self._scroll_signature:
                    return
                self._scroll_signature = signature
                self._left_gutter.configure(width=gutter)
                self._right_gutter.configure(width=gutter)
                pad = 16 + gutter / scale
                if pad != self._footer_pad:
                    self._footer_pad = pad
                    self._footer.grid(row=1, column=0, sticky='sew', padx=pad, pady=(0, 10))
                # Keep the embedded window as tall as the rows, or later sections get clipped.
                canvas.itemconfigure(self._window_id, width=width, height=content_height)
                canvas.configure(scrollregion=(0, 0, width, max(content_height, view_height)))
                thumb = '#3a3a40' if overflow else BG
                hover = '#52525b' if overflow else BG
                self._scrollbar.configure(button_color=thumb, button_hover_color=hover)
                if not overflow:
                    canvas.yview_moveto(0)
                if not self._scroll_dirty:
                    return
        finally:
            self._syncing_scroll = False

    def _is_descendant(self, widget, ancestor):
        while widget is not None:
            if widget == ancestor:
                return True
            widget = getattr(widget, 'master', None)
        return False

    def _hovered_widget(self):
        try:
            return self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        except tk.TclError:
            return None

    def _on_mousewheel(self, event):
        # Windows delivers the wheel to the focused widget. Scroll by what is under the pointer.
        hovered = self._hovered_widget()
        over_window = hovered is not None and (hovered is self or self._is_descendant(hovered, self))
        target = hovered if over_window else getattr(event, 'widget', None)
        if target is None or self._is_descendant(target, self.log):
            return
        if not over_window and not self._is_descendant(target, self._scroll_body):
            return
        if self._content.winfo_reqheight() <= self._canvas.winfo_height() + 1:
            return
        number = getattr(event, 'num', None)
        if number == 4:
            steps = -1
        elif number == 5:
            steps = 1
        else:
            delta = getattr(event, 'delta', 0) or 0
            if not delta:
                return
            steps = -1 if delta > 0 else 1
        self._canvas.yview_scroll(steps, 'units')
        return 'break'

    def destroy(self):
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            try:
                self.unbind_all(sequence)
            except tk.TclError:
                pass
        super().destroy()

    def _on_update(self):
        """BridgeWindow has no updater. App replaces this."""

    def show_update(self, display):
        """Idle check stays a small icon. A ready release becomes the yellow update pill."""
        if display.ready:
            text = display.label.replace('Install ', '更新 ')
            signature = ('ready', text)
            options = {'text': text, 'image': self._download_icon, 'fg_color': YELLOW, 'hover_color': '#ffcf1a',
                       'text_color': BG, 'width': 128, 'height': 28, 'border_width': 0, 'state': 'normal'}
            hint = ''
        else:
            hint = '' if display.label == 'Check for updates' else display.label
            state = 'disabled' if display.busy else 'normal'
            signature = ('idle', state, hint)
            options = {'text': '', 'image': self._refresh_icon, 'fg_color': '#27272b', 'hover_color': '#34343a',
                       'text_color': TEXT, 'width': 28, 'height': 28, 'border_width': 1,
                       'border_color': '#3e3e44', 'state': state}
        if self._update_signature != signature:
            self.update_btn.configure(**options)
            self._update_signature = signature
        if self.version_hint.cget('text') != hint:
            self.version_hint.configure(text=hint)

    def ask_cloud_credentials(self):
        """Read the keys already typed in this panel. Nothing is written to disk."""
        values = {name: widget.get().strip() for name, widget in self.credential_entries.items()}
        if not values['key'] or not values['email'] or not values['userKey']:
            if not self.storage_open:
                self._toggle_storage()
            self._mark_missing_keys(values)
            messagebox.showinfo('Connect echo3D', 'Enter the echo3D keys in Settings.', parent=self)
            return None
        if not messagebox.askokcancel('Upload to echo3D',
                                      'Upload every queued and new model to this collection?',
                                      parent=self):
            return None
        credentials = {'key': values['key'], 'email': values['email'], 'userKey': values['userKey']}
        if values['secKey']:
            credentials['secKey'] = values['secKey']
        self._key_attention = False
        for name in ('key', 'email', 'userKey'):
            self.credential_entries[name].configure(border_color=FIELD_BORDER)
        for widget in self.credential_entries.values():
            widget.delete(0, 'end')
        return credentials

    def _mark_missing_keys(self, values):
        self._key_attention = True
        first = None
        for name in ('key', 'email', 'userKey'):
            entry = self.credential_entries[name]
            empty = not values[name]
            entry.configure(border_color=RED if empty else FIELD_BORDER)
            if empty and first is None:
                first = entry
        if first is not None:
            first.focus()

    def _restyle_key_entry(self, entry):
        if self._key_attention:
            entry.configure(border_color=FIELD_BORDER if entry.get().strip() else RED)

    def note_echo3d(self, connected):
        self._echo_connected = bool(connected)
        icon = self._globe_icon if connected else self._echo_login_icon
        self._echo_status_icon.configure(image=icon)
        label = self.peer_labels.get('echo3d')
        if label is not None:
            label.configure(text_color=GREEN if connected else RED)

    def _bind_echo_status(self, status, label):
        def activate(_event=None):
            self.open_echo3d()
            return 'break'

        def hover(_event=None):
            label.configure(text_color='#fda4af' if not self._echo_connected else '#6ee7b7')

        def leave(_event=None):
            x, y = status.winfo_pointerxy()
            inside = (status.winfo_rootx() <= x < status.winfo_rootx() + status.winfo_width()
                      and status.winfo_rooty() <= y < status.winfo_rooty() + status.winfo_height())
            if not inside:
                self.note_echo3d(self._echo_connected)

        for widget in (status, label, self._echo_status_icon):
            widget.bind('<Button-1>', activate)
            widget.bind('<Enter>', hover)
            widget.bind('<Leave>', leave)
        for widget in (label, self._echo_status_icon):
            widget._canvas.configure(cursor='hand2')
            widget._label.configure(cursor='hand2')
        label.invoke = activate

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _sync_transfer_rows(self):
        # grid_forget, not grid_remove: CTk reapplies the last grid() on DPI
        # scaling, and grid_remove leaves that call in place, so the empty
        # filename and detail rows come back as a blank line.
        if self.phase_label.cget('text'):
            self.phase_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))
        else:
            self.phase_label.grid_forget()
        if self.transfer_var.get():
            self.transfer_label.grid(row=2, column=0, columnspan=2, sticky='w', pady=(2, 4))
        else:
            self.transfer_label.grid_forget()

    def _toggle_storage(self):
        self.storage_open = not self.storage_open
        if self.storage_open:
            self._settings_body.grid(row=2, column=0, sticky='ew', pady=(2, 0))
            self.cache_entry.grid(row=0, column=0, sticky='ew')
            self.storage_btn.configure(image=self._settings_open)
            reveal = self._settings_body
        else:
            self.cache_entry.grid_remove()
            self._settings_body.grid_remove()
            self.storage_btn.configure(image=self._settings_closed)
            reveal = None
        self._relayout(reveal)

    def _toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log.grid(row=2, column=0, sticky='ew', pady=(8, 0))
            self.log_btn.configure(image=self._settings_open)
            reveal = self.log_frame
        else:
            self.log.grid_remove()
            self.log_btn.configure(image=self._settings_closed)
            reveal = None
        self._relayout(reveal)

    def _relayout(self, reveal=None):
        self._scroll_signature = None
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        self._sync_scroll()
        self._reveal(reveal)

    def _reveal(self, widget):
        """Bring a section that was just opened into the viewport."""
        if widget is None:
            return
        try:
            if not widget.winfo_ismapped():
                return
        except tk.TclError:
            return
        bbox = self._canvas.bbox(self._window_id)
        if not bbox:
            return
        total = max(bbox[3] - bbox[1], 1)
        view = self._canvas.winfo_height()
        if total <= view + 1:
            return
        top = widget.winfo_rooty() - self._content.winfo_rooty()
        bottom = top + max(widget.winfo_height(), 1)
        origin = top if bottom - top >= view else bottom - view
        origin = max(0, min(origin, total - view))
        self._canvas.yview_moveto(origin / total)

    def _fit_opening_window(self):
        """Open around the padded panel. Width cannot grow past the column max."""
        self.geometry('400x560')
        try:
            self.update_idletasks()
            scale = float(self._get_window_scaling() or 1) or 1
            content = self._content.winfo_reqheight()
            footer = self._footer.winfo_reqheight()
        except tk.TclError:
            return
        if content < 80 or footer < 8:
            height = self._fit_height(560)
        else:
            natural = int((content + footer + 12) / scale)
            height = min(max(natural, 540), self._fit_height(620))
        self.geometry(f'400x{max(height, 320)}')
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        self._scroll_signature = None
        self._sync_scroll()
        self._schedule_link_lines()

    def _schedule_link_lines(self, _event=None):
        if getattr(self, '_link_line_job', None) or getattr(self, '_placing_links', False):
            return
        try:
            self._link_line_job = self.after_idle(self._place_link_lines)
        except tk.TclError:
            self._link_line_job = None

    def _place_link_lines(self):
        self._link_line_job = None
        icons = getattr(self, '_node_icons', ())
        lines = getattr(self, '_link_lines', ())
        if len(icons) != 3 or len(lines) != 2 or not self.winfo_exists():
            return
        self._placing_links = True
        try:
            try:
                self.update_idletasks()
            except tk.TclError:
                return
            for left, right, line in zip(icons, icons[1:], lines):
                try:
                    if left.winfo_width() <= 1 or right.winfo_width() <= 1:
                        return
                    span = right.winfo_x() - (left.winfo_x() + left.winfo_width())
                    gap = max(left.winfo_width(), int(span * 0.30))
                    x1 = left.winfo_x() + left.winfo_width() + gap
                    x2 = right.winfo_x() - gap
                    y = left.winfo_y() + max(0, (left.winfo_height() - 2) // 2)
                except tk.TclError:
                    return
                line.place(x=x1, y=y, width=max(8, x2 - x1), height=2)
                line.lift()
        finally:
            self._placing_links = False

    def _fit_height(self, desired):
        return min(desired, max(400, int(self.winfo_screenheight() / self._get_window_scaling()) - 100))

    def _set_progress_note(self, text, color=MUTED):
        self.transfer_hint.configure(text=text, text_color=color)

    def _set_sides(self, local, echo):
        self._local_progress = max(0.0, min(1.0, float(local)))
        self._echo_progress = max(0.0, min(1.0, float(echo)))
        self._draw_progress()

    def _draw_progress(self):
        canvas = self.progress_track
        try:
            width = canvas.winfo_width()
            height = canvas.winfo_height()
        except tk.TclError:
            return
        if width <= 2 or height <= 2:
            return
        canvas.delete('progress')
        amount = self._echo_progress if self._echo_progress > 0 else self._local_progress
        color = GREEN if self._echo_progress > 0 else YELLOW
        fill = max(0, width - 2) * amount
        if fill:
            canvas.create_rectangle(1, 1, 1 + fill, height - 1, fill=color, outline=color, tags='progress')

    def _apply_connection_headline(self):
        """One Tripo Studio status, the same wording as Blender."""
        studio = bool(getattr(self, 'connected', {}).get('studio'))
        if self._port_blocked:
            text, color = 'Port in use', RED
        elif self._listener_failed:
            text, color = 'Connection error', RED
        elif self._listener_ready and studio:
            text, color = 'Connected', GREEN
        elif self._listener_ready:
            text, color = 'Waiting for connection', YELLOW
        else:
            text, color = 'Disconnected', RED
        self.status_var.set(text)
        self.status_badge.configure(text_color=color)
        label = self.peer_labels.get('studio')
        if label is not None:
            label.configure(text=text, text_color=color)

    def show_service(self, ready):
        self._listener_ready = bool(ready)
        self._listener_failed = not ready
        self._port_blocked = False
        self._apply_connection_headline()
        if ready:
            self.phase_label.configure(text='', text_color=TEXT)
            self.transfer_var.set('')
            self.transfer_label.configure(text_color=MUTED)
            self._transfer_active = False
            self._set_sides(0, 0)
            self._set_progress_note('Models are saved locally, even while echo3D is offline.')
            self._sync_transfer_rows()

    def show_port_wait(self, port, retry_interval):
        self._port_blocked = True
        self._listener_ready = False
        self._listener_failed = False
        self._apply_connection_headline()
        self.phase_label.configure(text='', text_color=TEXT)
        self.transfer_var.set('Another application is using this port.')
        self.transfer_label.configure(text_color=RED)
        self._transfer_active = False
        self._set_sides(0, 0)
        self._set_progress_note(
            f'Close the other Bridge instance or application. Retrying automatically every {retry_interval:g}s; no restart needed.',
            RED)
        self._sync_transfer_rows()

    def show_progress(self, payload):
        percent = max(0, min(100, float(payload.get('percent', 0))))
        sending = payload.get('stage') == 'Sending to echo3D'
        self.phase_label.configure(text=payload.get('fileName') or '', text_color=TEXT)
        self.transfer_var.set('')
        self._transfer_active = True
        if sending:
            self._set_sides(1, percent / 100)
            note = 'Waiting for echo3D to confirm…' if percent == 100 else f'Uploading to echo3D… {percent:.0f}%'
            self._set_progress_note(note, GREEN)
        else:
            self._set_sides(percent / 100, 0)
            self._set_progress_note(f'Downloading from Tripo… {percent:.0f}%', YELLOW)
        self._sync_transfer_rows()

    def show_result(self, event, payload):
        success = bool(payload.get('success'))
        self.phase_label.configure(text=payload.get('fileName') or '', text_color=TEXT)
        self.transfer_var.set('')
        self._transfer_active = False
        if event == 'delivery':
            self._set_sides(1, 1 if success else 0)
            note = 'Uploaded to echo3D' if success else payload.get('message', 'Upload needs attention')
            self._set_progress_note(note, GREEN if success else RED)
        else:
            self._set_sides(1 if success else self._local_progress, 0)
            note = ('Saved on this computer. Upload starts when echo3D is connected.' if success
                    else payload.get('message', 'Download needs attention'))
            self._set_progress_note(note, YELLOW if success else RED)
        self._sync_transfer_rows()
