"""The cosmetic patch uses actual Tk controls; no normative data is changed."""
from __future__ import annotations
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import test_generation_gui as base
from greencad_editor.generation import GenerationOptions, GenerationTarget
from greencad_editor.checks_ui import RulePicker


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt','Требуется Tk/Xvfb')
class NaturalGenerationGuiTests(unittest.TestCase):
    setUp=base.GenerationGuiTests.setUp
    tearDown=base.GenerationGuiTests.tearDown
    pump=base.GenerationGuiTests.pump
    wait=base.GenerationGuiTests.wait
    click=base.GenerationGuiTests.click

    def dialog(self):
        self.click(self.ctl.build_button)
        return self.ctl.options_window

    def test_mode_variant_and_real_generation_button(self):
        d=self.dialog()
        self.assertEqual(d.layout_keys[d.layout_combo.current()],'natural')
        self.click(d.next_button);self.assertEqual(d.seed.get(),'2')
        d.cap.set('5');d.step.set('3');self.click(d.build_button);self.wait()
        self.assertEqual(self.ctl.result.options.seed,2)
        self.assertEqual(self.ctl.result.options.layout,'natural')
        self.assertTrue(self.ctl.result.can_apply)
        self.assertIn('вариант 2',self.ctl.status.get())
        self.assertFalse(self.app.session.placements)

    def test_old_mode_selectable_seed_disabled(self):
        d=self.dialog();d.layout_combo.current(d.layout_keys.index('grid'))
        d.layout_combo.event_generate('<<ComboboxSelected>>');self.pump()
        self.assertEqual(str(d.seed_entry.cget('state')),'disabled')
        self.assertEqual(str(d.next_button.cget('state')),'disabled')
        d.step.set('4');self.click(d.build_button);self.wait()
        self.assertEqual(self.ctl.result.options.layout,'grid')

    def test_reopen_dialog_preserves_variant_and_report_explains_it(self):
        self.ctl.start(GenerationOptions('green_01',(GenerationTarget('T1',4),),2,'natural',52))
        self.wait();d=self.dialog();self.assertEqual(d.seed.get(),'52')
        self.assertEqual(d.layout_keys[d.layout_combo.current()],'natural')
        self.click(d.cancel_button);self.click(self.ctl.review_button)
        text=self.ctl.review_window.pages['summary'].get('1.0','end')
        self.assertIn('52',text);self.assertIn('Свободная',text)

    def test_invalid_seed_stays_in_dialog_and_changes_nothing(self):
        before=self.app.session.state();d=self.dialog();d.seed.set('-5')
        self.click(d.build_button)
        self.assertTrue(d.winfo_exists());self.assertEqual(self.app.session.state(),before)
        self.assertEqual(len(self.errors),1);self.errors.clear()

    def test_buttons_remain_visible_with_scroll_form(self):
        self.root.tk.call('tk','scaling',2.0);d=self.dialog()
        for geometry in ('820x520','1000x760'):
            d.geometry(geometry);self.pump()
            for b in (d.build_button,d.cancel_button):
                self.assertGreater(b.winfo_height(),1)
                self.assertLessEqual(b.winfo_rooty()+b.winfo_height(),d.winfo_rooty()+d.winfo_height())
            d.form_scroll.canvas.yview_moveto(1);self.pump()
            self.assertLessEqual(d.build_button.winfo_rooty()+d.build_button.winfo_height(),d.winfo_rooty()+d.winfo_height())

    def test_yellow_banner_removed_but_test_disclosure_remains(self):
        def all_children(w):
            for child in w.winfo_children():
                yield child;yield from all_children(child)
        texts=[]
        for w in all_children(self.root):
            try:texts.append(str(w.cget('text')))
            except Exception:pass
        self.assertFalse(any('РЕДАКТОР И ГЕНЕРАТОР' in t for t in texts))
        d=RulePicker(self.root,self.app.session,lambda *a:None);self.pump()
        texts=[]
        for w in all_children(d):
            try:texts.append(str(w.cget('text')))
            except Exception:pass
        self.assertTrue(any('тестовые параметры' in t for t in texts))
        d.destroy()

if __name__=='__main__':unittest.main()
