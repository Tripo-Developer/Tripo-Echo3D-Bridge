"""Native widget regression checks; require a desktop/display session."""
import tempfile
import gc
import unittest
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk

from desktop_app import App
from desktop_ui import GREEN, RED, YELLOW
from echo3d_bridge.settings import clear_cached_transfers


class DesktopTests(unittest.TestCase):
    def test_event_states_and_actions(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('desktop_app.load_inbox', return_value=Path(folder)), \
                patch('desktop_app.RelayRuntime.start'), \
                patch('desktop_app.RelayRuntime.stop'), \
                patch('desktop_app.Updater.start_check'), \
                patch('desktop_app.messagebox.showerror') as error_dialog:
            app = App()
            app.withdraw()
            try:
                def emit(event, **payload):
                    app.events.put((event, payload))
                    app._drain_events()

                self.assertFalse(app.demo_ready)
                self.assertEqual(app.version_hint.cget('text'), '')
                self.assertEqual(str(app.update_btn.cget('state')), 'normal')
                emit('port_in_use', port=60710, retryInterval=3)
                self.assertIn('Port in use', app.status_var.get())
                self.assertEqual(app.status_badge.cget('text_color'), RED)
                self.assertEqual(app.peer_labels['studio'].cget('text_color'), RED)
                self.assertEqual(app.peer_labels['studio'].cget('text'), 'Port in use')
                self.assertFalse(app.demo_ready)
                self.assertIn('no restart needed', app.transfer_hint.cget('text'))
                error_dialog.assert_not_called()
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['studio'].invoke()
                    app.peer_links['echo3d'].invoke()
                    browser.assert_any_call('https://studio.tripo3d.ai/workspace/generate')
                    browser.assert_any_call('https://www.echo3d.com')
                emit('ready')
                self.assertEqual(app.status_var.get(), 'Waiting for connection')
                self.assertEqual(app.status_badge.cget('text_color'), YELLOW)
                self.assertEqual(app.phase_label.cget('text'), '')
                self.assertEqual(app.transfer_var.get(), '')
                self.assertEqual(app._local_progress, 0)
                self.assertEqual(app._echo_progress, 0)
                self.assertEqual(app.transfer_hint.cget('text'), 'Models are saved locally, even while echo3D is offline.')
                self.assertNotIn('retry', app.transfer_hint.cget('text').lower())
                self.assertTrue(app.demo_ready)
                emit('connections', studioConnected=True, echo3dConnected=False)
                self.assertEqual(app.status_var.get(), 'Connected')
                self.assertEqual(app.status_badge.cget('text_color'), GREEN)
                self.assertIn('Connected', app.peer_labels['studio'].cget('text'))
                self.assertIn('Not connected', app.peer_labels['echo3d'].cget('text'))

                name = '模型_' + 'long-name-' * 20 + '.glb'
                emit('progress', fileName=name, percent=50, stage='Saving')
                self.assertEqual(app._local_progress, .5)
                self.assertEqual(app._echo_progress, 0)
                self.assertEqual(app.transfer_hint.cget('text_color'), YELLOW)
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_hint.cget('text'), 'Downloading from Tripo… 50%')
                app.update()
                local_fill = app.progress_track.coords(app.progress_track.find_withtag('progress')[0])
                self.assertGreater(local_fill[2] - local_fill[0], app.progress_track.winfo_width() * 0.4)
                emit('result', success=True, fileName=name, localSaved=True)
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_hint.cget('text'), 'Saved on this computer. Upload starts when echo3D is connected.')
                self.assertEqual(app._local_progress, 1)
                emit('progress', fileName=name, percent=100, stage='Sending to echo3D')
                self.assertIn('confirm', app.transfer_hint.cget('text'))
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_hint.cget('text'), 'Waiting for echo3D to confirm…')
                self.assertEqual(app._echo_progress, 1)
                self.assertEqual(app.transfer_hint.cget('text_color'), GREEN)
                app.update()
                echo_fill = app.progress_track.coords(app.progress_track.find_withtag('progress')[0])
                self.assertGreater(echo_fill[2] - echo_fill[0], app.progress_track.winfo_width() * 0.9)
                self.assertEqual(app.progress_track.itemcget('progress', 'fill'), GREEN)
                emit('delivery', success=False, fileName=name, message='Receiver rejected the model.')
                self.assertEqual(app.transfer_hint.cget('text'), 'Receiver rejected the model.')
                self.assertEqual(app.transfer_hint.cget('text_color'), RED)
                emit('delivery', success=True, fileName=name)
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_hint.cget('text'), 'Uploaded to echo3D')
                self.assertEqual(app._echo_progress, 1)

                log_text = app.log.get('1.0', 'end')
                self.assertIn('Bridge started.', log_text)
                self.assertIn('Server running on port: 60710', log_text)
                app.log.configure(state='normal')
                app.log.insert('end', 'scroll-check\n' * 80)
                app.log.configure(state='disabled')
                app.update_idletasks()
                app.log._check_if_scrollbars_needed()
                self.assertTrue(app.log._y_scrollbar.grid_info())
                app.clear_log()
                self.assertEqual(app.log.get('1.0', 'end').strip(), '')
                self.assertFalse(any(isinstance(child, ctk.CTkScrollableFrame) for child in app.winfo_children()))
                self.assertEqual(app._footer.master, app)
                self.assertEqual(int(app._footer.grid_info()['row']), 1)
                app.geometry('440x320+-3000+-3000')
                app.deiconify()
                app.update()
                app._scroll_signature = None
                app._sync_scroll()
                app.update()
                header = app._content.grid_slaves(row=0, column=0)[0]
                app.update_idletasks()
                right_gap = app._content.winfo_width() - header.winfo_x() - header.winfo_width()
                self.assertAlmostEqual(header.winfo_x(), right_gap, delta=2)
                self.assertEqual(app._content.winfo_width(), app._canvas.winfo_width())
                self.assertGreater(app._content.winfo_reqheight(), app._canvas.winfo_height())
                self.assertEqual(app._left_gutter.winfo_width(), app._right_gutter.winfo_width())
                self.assertGreater(app._left_gutter.winfo_width(), 1)

                def right_edge(widget):
                    return widget.winfo_rootx() + widget.winfo_width()

                self.assertAlmostEqual(right_edge(app.update_btn), right_edge(app.clear_log_btn), delta=2)
                self.assertAlmostEqual(app.update_btn.winfo_height(), app.clear_log_btn.winfo_height(), delta=1)
                wheel = type('Wheel', (), {'widget': app._canvas, 'delta': -120, 'num': 0})()
                app._on_mousewheel(wheel)
                self.assertGreater(app._canvas.yview()[0], 0)
                held = app._canvas.yview()[0]
                app._on_mousewheel(type('Wheel', (), {'widget': app.log, 'delta': -120, 'num': 0})())
                self.assertEqual(app._canvas.yview()[0], held)
                self.assertTrue(app.log_open)
                app.log_btn.invoke()
                self.assertFalse(app.log_open)
                self.assertEqual(app.log.grid_info(), {})
                app.log_btn.invoke()
                self.assertFalse(app.storage_open)
                self.assertEqual(app.cache_entry.grid_info(), {})
                app.storage_btn.invoke()
                app.update()
                self.assertTrue(app.storage_open)
                self.assertTrue(app.cache_entry.grid_info())
                self.assertAlmostEqual(right_edge(app.clear_cache_btn), right_edge(app.update_btn), delta=2)
                kept = Path(folder) / 'active'
                gone = Path(folder) / 'done'
                kept.mkdir()
                (kept / 'model.glb').write_bytes(b'keep')
                gone.mkdir()
                (gone / 'model.glb').write_bytes(b'drop')
                (Path(folder) / 'note.txt').write_text('stay', encoding='utf-8')
                with patch('desktop_app.messagebox.askyesno', return_value=False) as confirm:
                    app.clear_cache_btn.invoke()
                    confirm.assert_called_once()
                self.assertTrue(gone.exists())
                with patch('desktop_app.messagebox.askyesno', return_value=True):
                    app.clear_cache_btn.invoke()
                app._drain_events()
                self.assertFalse(gone.exists())
                self.assertFalse(kept.exists())
                self.assertEqual((Path(folder) / 'note.txt').read_text(encoding='utf-8'), 'stay')
                self.assertTrue(Path(folder).is_dir())
                self.assertIn('Cache clear removed', app.log.get('1.0', 'end'))
                app.storage_btn.invoke()
                self.assertFalse(app.storage_open)
                with patch('desktop_app.filedialog.askdirectory', return_value=folder), \
                        patch.object(app.runtime, 'change_inbox') as change:
                    app.choose_inbox()
                    change.assert_called_once_with(Path(folder))
                    self.assertEqual(app.change_cache_btn.cget('state'), 'disabled')
                emit('cache_changed', path=folder)
                self.assertEqual(app.cache_var.get(), folder)
                self.assertEqual(app.change_cache_btn.cget('state'), 'normal')
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['studio'].invoke()
                    browser.assert_called_once_with('https://studio.tripo3d.ai/workspace/generate')
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['echo3d'].invoke()
                    browser.assert_called_once_with('https://www.echo3d.com')
                secret = 'collection-secret'
                with patch.object(app, 'ask_cloud_credentials', return_value=None) as ask, \
                        patch.object(app.runtime, 'start_cloud') as start:
                    app.connect_btn.invoke()
                    ask.assert_called_once()
                    start.assert_not_called()
                credentials = {'key': secret, 'email': 'a@b.c', 'userKey': 'user-secret'}
                with patch.object(app, 'ask_cloud_credentials', return_value=credentials), \
                        patch.object(app.runtime, 'start_cloud', return_value=True) as start:
                    app.connect_btn.invoke()
                    start.assert_called_once_with(credentials)
                    self.assertNotIn(secret, app.log.get('1.0', 'end'))
                with patch.object(app.runtime, 'cloud_active', return_value=True), \
                        patch('desktop_app.messagebox.askyesno', return_value=True) as confirm, \
                        patch.object(app.runtime, 'stop_cloud') as stop:
                    app.connect_btn.invoke()
                    confirm.assert_called_once()
                    stop.assert_called_once_with()
                emit('error', message='Port already in use')
                error_dialog.assert_called_once()
                self.assertEqual(app.status_var.get(), 'Connection error')
                self.assertFalse(app.demo_ready)
                with patch('desktop_app.webbrowser.open') as browser, \
                        patch.object(app.runtime, 'start_cloud') as start:
                    app.peer_links['echo3d'].invoke()
                    app.connect_btn.invoke()
                    browser.assert_called_once_with('https://www.echo3d.com')
                    start.assert_not_called()
            finally:
                app.runtime.loop.close()
                app.destroy()
                del app
                gc.collect()


class ScrollTests(unittest.TestCase):
    def test_settings_then_log_stays_on_screen(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('desktop_app.load_inbox', return_value=Path(folder)), \
                patch('desktop_app.RelayRuntime.start'), \
                patch('desktop_app.RelayRuntime.stop'), \
                patch('desktop_app.Updater.start_check'):
            app = App()
            try:
                app.geometry('440x360+-3000+-3000')
                app.deiconify()
                app.update()
                app.log_btn.invoke()
                self.assertFalse(app.log_open)
                app.storage_btn.invoke()
                app.update()
                self.assertTrue(app.storage_open)
                region = [float(value) for value in app._canvas.cget('scrollregion').split()]
                self.assertGreaterEqual(region[3], app._content.winfo_reqheight() - 2)
                app.log_btn.invoke()
                app.update()
                self.assertTrue(app.log_open)
                region = [float(value) for value in app._canvas.cget('scrollregion').split()]
                log_bottom = app.log.winfo_rooty() - app._content.winfo_rooty() + app.log.winfo_height()
                self.assertGreaterEqual(region[3], log_bottom - 2)
                visible_top = app._canvas.winfo_rooty()
                visible_bottom = visible_top + app._canvas.winfo_height()
                self.assertLess(app.log.winfo_rooty() + 20, visible_bottom)
                self.assertGreater(app.log.winfo_rooty() + app.log.winfo_height() - 8, visible_top)
                app._canvas.yview_moveto(0)
                with patch.object(app, 'winfo_containing', return_value=app):
                    app._on_mousewheel(type('Wheel', (), {'widget': app, 'delta': -120, 'num': 0})())
                self.assertGreater(app._canvas.yview()[0], 0)
            finally:
                app.runtime.loop.close()
                app.destroy()
                del app
                gc.collect()


class CacheClearTests(unittest.TestCase):
    def test_keeps_root_files_and_active_transfer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gone = root / 'done'
            gone.mkdir()
            (gone / 'model.glb').write_bytes(b'drop')
            kept = root / 'active'
            kept.mkdir()
            active = kept / 'model.part'
            active.write_bytes(b'keep')
            (root / 'note.txt').write_text('stay', encoding='utf-8')
            removed = clear_cached_transfers(root, [active])
            self.assertEqual(removed, 1)
            self.assertFalse(gone.exists())
            self.assertTrue(active.exists())
            self.assertEqual((root / 'note.txt').read_text(encoding='utf-8'), 'stay')
            self.assertTrue(root.is_dir())


if __name__ == '__main__':
    unittest.main()
