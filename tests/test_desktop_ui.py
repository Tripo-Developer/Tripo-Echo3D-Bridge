"""Native widget regression checks; require a desktop/display session."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk

from desktop_app import App


class DesktopTests(unittest.TestCase):
    def test_event_states_and_actions(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('desktop_app.load_inbox', return_value=Path(folder)), \
                patch('desktop_app.RelayRuntime.start'), \
                patch('desktop_app.RelayRuntime.stop'), \
                patch('desktop_app.messagebox.showerror') as error_dialog:
            app = App()
            app.withdraw()
            try:
                def emit(event, **payload):
                    app.events.put((event, payload))
                    app._drain_events()

                self.assertFalse(app.demo_ready)
                emit('port_in_use', port=60710, retryInterval=3)
                self.assertIn('Port in use', app.status_var.get())
                self.assertEqual(app.status_badge.cget('text_color'), '#F38B88')
                self.assertEqual(app.peer_labels['local'].cget('text_color'), '#F38B88')
                self.assertIn('Waiting for port', app.peer_labels['local'].cget('text'))
                self.assertFalse(app.demo_ready)
                self.assertIn('no restart needed', app.transfer_hint.cget('text'))
                error_dialog.assert_not_called()
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['studio'].invoke()
                    app.peer_links['echo3d'].invoke()
                    browser.assert_not_called()
                emit('ready')
                self.assertIn('Running', app.status_var.get())
                self.assertEqual(app.phase_label.cget('text'), '')
                self.assertEqual(app.transfer_var.get(), '')
                self.assertEqual(app.progress.get(), 0)
                self.assertEqual(app.progress.cget('progress_color'), app.progress.cget('fg_color'))
                self.assertNotIn('retry', app.transfer_hint.cget('text').lower())
                self.assertTrue(app.demo_ready)
                emit('connections', studioConnected=True, echo3dConnected=False)
                self.assertIn('Connected', app.peer_labels['studio'].cget('text'))
                self.assertIn('Not connected', app.peer_labels['echo3d'].cget('text'))

                name = '模型_' + 'long-name-' * 20 + '.glb'
                emit('progress', fileName=name, percent=50, stage='Saving')
                self.assertEqual(app.progress.get(), .5)
                self.assertEqual(app.progress.cget('progress_color'), '#F9CF00')
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_var.get(), 'Saving to your computer')
                emit('result', success=True, fileName=name, localSaved=True)
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_var.get(), 'Saved to your computer')
                emit('progress', fileName=name, percent=100, stage='Sending to echo3D')
                self.assertIn('confirmation', app.transfer_hint.cget('text'))
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_var.get(), 'Delivering to echo3D')
                emit('delivery', success=False, fileName=name, message='Receiver rejected the model.')
                self.assertEqual(app.percent_label.cget('text'), 'Failed')
                self.assertEqual(app.transfer_hint.cget('text'), 'Receiver rejected the model.')
                self.assertEqual(app.transfer_var.get(), 'Transfer needs attention')
                emit('delivery', success=True, fileName=name)
                self.assertEqual(app.phase_label.cget('text'), name)
                self.assertEqual(app.transfer_var.get(), 'Delivered to echo3D')

                log_text = app.log.get('1.0', 'end')
                self.assertIn('Bridge started.', log_text)
                self.assertIn('Local connection · port 60710', log_text)
                app.log.configure(state='normal')
                app.log.insert('end', 'scroll-check\n' * 80)
                app.log.configure(state='disabled')
                app.update_idletasks()
                app.log._check_if_scrollbars_needed()
                self.assertTrue(app.log._y_scrollbar.grid_info())
                app.clear_log()
                self.assertEqual(app.log.get('1.0', 'end').strip(), '')
                self.assertFalse(any(isinstance(child, ctk.CTkScrollableFrame) for child in app.winfo_children()))
                self.assertFalse(app.storage_open)
                self.assertEqual(app.cache_entry.grid_info(), {})
                app.storage_btn.invoke()
                self.assertTrue(app.storage_open)
                self.assertTrue(app.cache_entry.grid_info())
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
                    browser.assert_called_once_with('http://127.0.0.1:60710/bridge-client.html')
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['echo3d'].invoke()
                    browser.assert_called_once_with('http://127.0.0.1:60710/demo')
                emit('error', message='Port already in use')
                error_dialog.assert_called_once()
                self.assertIn('Unavailable', app.status_var.get())
                self.assertFalse(app.demo_ready)
                with patch('desktop_app.webbrowser.open') as browser:
                    app.peer_links['echo3d'].invoke()
                    browser.assert_not_called()
            finally:
                app.runtime.loop.close()
                app.destroy()


if __name__ == '__main__':
    unittest.main()
