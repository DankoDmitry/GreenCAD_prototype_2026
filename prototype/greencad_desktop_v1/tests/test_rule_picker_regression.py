"""Regression: dialog actions must remain clickable, and settings must persist.

Uses real Tk widgets/events. Linux: xvfb-run -a python -m unittest discover -s tests -v
The explicit Tk scale stresses layout; it is NOT a native Windows DPI test.
"""
from __future__ import annotations

from copy import deepcopy
import gc
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from greencad_editor.checks_ui import RulePicker
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.project_io import load_project, save_project
from greencad_editor.session import EditorSession
from greencad_editor.ui import EditorApp


@unittest.skipUnless(sys.platform in ('win32', 'darwin') or os.environ.get('DISPLAY'),
                     'Требуется графическая сессия или Xvfb')
class RulePickerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.root = tk.Tk()
        self.root.tk.call('tk', 'scaling', 2.0)
        self.app = EditorApp(self.root)
        self.errors = []
        self.root.report_callback_exception = lambda typ, value, tb: self.errors.append(value)
        drawing = load_dxf(make_demo(self.folder / 'input.dxf'))
        self.app.adopt(EditorSession(drawing))
        self.pump()

    def tearDown(self):
        self.app.executor.shutdown(wait=True)
        self.root.destroy()
        self.tmp.cleanup()
        self.app = None
        self.root = None
        gc.collect()
        self.assertFalse(self.errors, repr(self.errors))

    def pump(self):
        for _ in range(4):
            self.root.update()

    def picker(self):
        # Exercise the production controller callback, not a mock save function.
        self.app.validation_controller.choose()
        self.pump()
        return next(w for w in self.root.winfo_children() if isinstance(w, RulePicker))

    def assert_visible(self, widget, dialog):
        self.assertTrue(widget.winfo_ismapped())
        self.assertGreater(widget.winfo_width(), 1)
        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
        x = widget.winfo_rootx() - dialog.winfo_rootx()
        y = widget.winfo_rooty() - dialog.winfo_rooty()
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + widget.winfo_width(), dialog.winfo_width())
        self.assertLessEqual(y + widget.winfo_height(), dialog.winfo_height())
        hit = dialog.winfo_containing(widget.winfo_rootx() + widget.winfo_width() // 2,
                                      widget.winfo_rooty() + widget.winfo_height() // 2)
        self.assertIs(hit, widget, 'Кнопка перекрыта другой частью окна')

    def click(self, widget):
        widget.event_generate('<Enter>')
        x, y = widget.winfo_width() // 2, widget.winfo_height() // 2
        widget.event_generate('<ButtonPress-1>', x=x, y=y)
        widget.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.pump()

    def wm_close(self, dialog):
        dialog.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
        self.pump()

    def test_footer_visible_on_every_tab_at_minimum_and_default_sizes(self):
        dlg = self.picker()
        for size in ('850x620', '960x730', '1100x800'):
            dlg.geometry(size)
            for tab in dlg.tabs.tabs():
                with self.subTest(size=size, tab=tab):
                    dlg.tabs.select(tab)
                    self.pump()
                    self.assert_visible(dlg.done_button, dlg)
                    self.assert_visible(dlg.cancel_button, dlg)

    def test_real_done_click_reopen_and_gcp_roundtrip(self):
        dlg = self.picker()
        dlg.geometry('850x620')
        self.pump()
        rid = 'project.gas'
        before = rid in self.app.session.check_settings['enabled']
        dlg.tree.see(rid)
        self.pump()
        x, y, w, h = dlg.tree.bbox(rid, 'on')
        dlg.tree.event_generate('<ButtonPress-1>', x=x+w//2, y=y+h//2)
        dlg.tree.event_generate('<ButtonRelease-1>', x=x+w//2, y=y+h//2)
        self.pump()
        dlg.a1.set('0,5')
        dlg.threshold_vars['project.gas'].set('4,25')
        self.assertEqual(before, rid in self.app.session.check_settings['enabled'])
        self.assert_visible(dlg.done_button, dlg)
        self.click(dlg.done_button)
        self.assertFalse(dlg.winfo_exists())
        self.assertNotEqual(before, rid in self.app.session.check_settings['enabled'])
        self.assertTrue(self.app.session.dirty)
        expected = deepcopy(self.app.session.check_settings)
        self.assertEqual(expected['site']['A1'], .5)
        self.assertEqual(expected['thresholds_m']['project.gas'], 4.25)
        reopened = self.picker()
        self.assertEqual(set(expected['enabled']), reopened.selected)
        self.assertEqual('0.5', reopened.a1.get())
        self.click(reopened.done_button)
        target = self.folder / 'saved.gcp'
        save_project(self.app.session, target)
        restored, _ = load_project(target)
        self.assertEqual(restored.check_settings, expected)
        self.assertFalse(restored.dirty)

    def test_explicit_cancel_discards_draft(self):
        before = self.app.session.state()
        dlg = self.picker()
        dlg.select_none()
        dlg.a1.set('0.8')
        dlg.lo.set('.2'); dlg.hi.set('.7'); dlg.store_range()
        self.click(dlg.cancel_button)
        self.assertEqual(self.app.session.state(), before)

    def test_wm_close_can_apply(self):
        dlg = self.picker(); dlg.select_none()
        with patch('greencad_editor.checks_ui.messagebox.askyesnocancel', return_value=True) as ask:
            self.wm_close(dlg)
        ask.assert_called_once()
        self.assertFalse(dlg.winfo_exists())
        self.assertEqual(self.app.session.check_settings['enabled'], [])

    def test_wm_close_can_discard(self):
        before = self.app.session.state()
        dlg = self.picker(); dlg.select_none()
        with patch('greencad_editor.checks_ui.messagebox.askyesnocancel', return_value=False):
            self.wm_close(dlg)
        self.assertFalse(dlg.winfo_exists())
        self.assertEqual(self.app.session.state(), before)

    def test_wm_close_can_return_to_editing(self):
        before = self.app.session.state()
        dlg = self.picker(); dlg.select_none()
        with patch('greencad_editor.checks_ui.messagebox.askyesnocancel', return_value=None):
            self.wm_close(dlg)
        self.assertTrue(dlg.winfo_exists())
        self.assertEqual(dlg.selected, set())
        self.assertEqual(self.app.session.state(), before)

    def test_unchanged_dialog_closes_without_prompt_or_dirty_state(self):
        before = self.app.session.state()
        dlg = self.picker()
        with patch('greencad_editor.checks_ui.messagebox.askyesnocancel') as ask:
            self.wm_close(dlg)
        ask.assert_not_called()
        self.assertEqual(self.app.session.state(), before)
        self.assertFalse(self.app.session.dirty)
        dlg = self.picker()
        self.click(dlg.done_button)
        self.assertEqual(self.app.session.state(), before)
        self.assertFalse(self.app.session.dirty)

    def test_invalid_input_keeps_dialog_and_project_intact(self):
        before = self.app.session.state()
        dlg = self.picker(); dlg.select_none(); dlg.a1.set('1.5')
        with patch('greencad_editor.checks_ui.messagebox.showerror') as error:
            self.click(dlg.done_button)
        error.assert_called_once()
        self.assertTrue(dlg.winfo_exists())
        self.assertEqual(self.app.session.state(), before)
        self.assertEqual(dlg.selected, set())

    def test_ctrl_enter_applies_and_escape_asks(self):
        dlg = self.picker(); dlg.select_none()
        dlg.tree.focus_force(); self.pump()
        dlg.tree.event_generate('<Control-Return>'); self.pump()
        self.assertFalse(dlg.winfo_exists())
        self.assertEqual(self.app.session.check_settings['enabled'], [])
        dlg = self.picker(); dlg.select_all()
        dlg.tree.focus_force(); self.pump()
        with patch('greencad_editor.checks_ui.messagebox.askyesnocancel', return_value=None) as ask:
            dlg.tree.event_generate('<Escape>'); self.pump()
        ask.assert_called_once()
        self.assertTrue(dlg.winfo_exists())

    def test_long_parameter_form_is_scrollable_without_moving_footer(self):
        dlg = self.picker(); dlg.geometry('850x620'); dlg.tabs.select(dlg.params_tab)
        self.pump()
        pane = dlg.params_tab
        self.assertLess(pane.canvas.yview()[1], 1.0)
        footer_y = dlg.done_button.winfo_rooty()
        pane.canvas.yview_moveto(1.0); self.pump()
        self.assertGreater(pane.canvas.yview()[0], 0.0)
        self.assertEqual(pane.canvas.yview()[1], 1.0)
        self.assertEqual(dlg.done_button.winfo_rooty(), footer_y)
        self.assert_visible(dlg.done_button, dlg)


if __name__ == '__main__':
    unittest.main()
