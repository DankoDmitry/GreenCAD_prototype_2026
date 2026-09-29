from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from greencad_editor.admissibility import MapOptions, TOTAL
from greencad_editor.admissibility_ui import MapOptionsDialog
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.project_io import save_project, load_project
from greencad_editor.session import EditorSession
from greencad_editor.ui import EditorApp


@unittest.skipUnless(sys.platform in ('win32','darwin') or os.environ.get('DISPLAY'), 'Требуется Tk/Xvfb')
class MapGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.folder=Path(self.tmp.name)
        self.root=tk.Tk(); self.app=EditorApp(self.root); self.errors=[]
        self.root.report_callback_exception=lambda typ,value,tb:self.errors.append(value)
        self.patch=patch('greencad_editor.ui.messagebox.showerror',side_effect=lambda *a,**k:self.errors.append(a))
        self.patch.start()
        self.path=make_demo(self.folder/'input.dxf')
        self.app.adopt(EditorSession(load_dxf(self.path))); self.ctl=self.app.admissibility_controller
        self.pump()

    def tearDown(self):
        self.app.executor.shutdown(wait=True)
        self.ctl.shutdown()
        self.root.destroy(); self.tmp.cleanup(); self.patch.stop()
        self.app=self.root=self.ctl=None; gc.collect()
        self.assertFalse(self.errors,repr(self.errors))

    def pump(self,n=8):
        for _ in range(n): self.root.update(); time.sleep(.005)

    def wait(self):
        start=time.monotonic()
        while self.app.busy and time.monotonic()-start<15:self.pump()
        self.assertFalse(self.app.busy)
        self.pump(18)

    def build(self,**kwargs):
        options=MapOptions('T1',step_m=4,**kwargs)
        self.ctl.start(options); self.wait()
        self.assertEqual(self.ctl.state(),'CURRENT')

    def click(self,w):
        w.event_generate('<Enter>')
        w.event_generate('<ButtonPress-1>',x=w.winfo_width()//2,y=w.winfo_height()//2)
        w.event_generate('<ButtonRelease-1>',x=w.winfo_width()//2,y=w.winfo_height()//2)
        self.pump()

    def test_real_build_button_and_no_placements(self):
        before=deepcopy(self.app.session.state())
        self.click(self.ctl.build_button)
        dlg=self.ctl.options_window
        self.assertIsInstance(dlg,MapOptionsDialog)
        dlg.step.set('4'); self.click(dlg.build_button); self.wait()
        self.assertEqual(self.app.session.state(),before)
        self.assertEqual(self.ctl.state(),'CURRENT')
        self.assertTrue(self.app.canvas.find_withtag('map_status:PASS'))
        self.assertTrue(self.app.canvas.find_withtag('map_status:FAIL'))
        self.assertEqual(self.app.canvas.mode,'inspect')

    def test_layer_switch_reuses_grid(self):
        self.build(); result=self.ctl.result
        self.ctl.layer_combo.current(self.ctl._layer_ids.index('R.domain'))
        self.ctl.layer_combo.event_generate('<<ComboboxSelected>>'); self.pump()
        self.assertIs(self.ctl.result,result)
        self.assertFalse(self.app.canvas.find_withtag('map_status:FAIL'))
        self.ctl.layer_combo.current(self.ctl._layer_ids.index('project.gas'))
        self.ctl.change_layer();self.pump()
        self.assertTrue(self.app.canvas.find_withtag('map_status:FAIL'))

    def test_real_click_exact_not_cell_and_explanation(self):
        self.build();canvas=self.app.canvas
        x,y=canvas.to_screen(40,31)
        canvas.event_generate('<ButtonPress-1>',x=round(x),y=round(y))
        canvas.event_generate('<ButtonRelease-1>',x=round(x),y=round(y))
        self.wait()
        row=self.ctl.last_inspection
        self.assertEqual(row['status'],'FAIL')
        self.assertIn('gas_01',self.app.info.get('1.0','end'))
        self.assertFalse(self.app.editor.winfo_ismapped())
        self.assertGreater(self.app.info.winfo_height(),200)
        self.assertAlmostEqual(row['x'],canvas.to_world(round(x),round(y))[0])
        self.assertFalse(self.app.session.placements)
        self.ctl.layer_combo.current(self.ctl._layer_ids.index('R.domain'))
        self.ctl.change_layer();self.pump()
        self.assertIn('ТОЛЬКО ОДНО ПРАВИЛО',self.app.info.get('1.0','end'))
        self.assertIn('Выполнено',self.app.selection_caption.cget('text'))

    def test_stale_hidden_and_cannot_inspect(self):
        self.build()
        settings=deepcopy(self.app.session.check_settings)
        settings['thresholds_m']['project.gas']=6
        self.app.session.set_check_settings(settings);self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'STALE')
        self.assertFalse(self.app.canvas.find_withtag('admissibility'))
        self.ctl.inspect(40,31); self.assertIsNone(self.ctl.last_inspection)
        self.assertIn('УСТАРЕЛА',self.ctl.status.get())
        self.app.session.undo();self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'CURRENT')
        self.assertTrue(self.app.canvas.find_withtag('admissibility'))

    def test_view_and_existing_plant_move_keep_static_map(self):
        self.build();result=self.ctl.result
        self.app.canvas.zoom(1.25)
        self.app.canvas.hidden_layers.add('GC_GAS_AXIS')
        self.pump()
        p=self.app.session.add('T1',20,10);self.app.on_change()
        self.app.session.move(p.id,40,31);self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'CURRENT')
        self.assertIs(self.ctl.result,result)
        self.assertTrue(self.app.canvas.find_withtag('map_status:FAIL'))
        self.assertEqual(self.ctl.result.kernel.inspect(40,31)['status'],'FAIL')

    def test_hide_and_show_no_recompute(self):
        self.build(); result=self.ctl.result
        self.ctl.visible.set(False); self.ctl.toggle(); self.pump()
        self.assertFalse(self.app.canvas.find_withtag('admissibility'))
        self.assertEqual(self.app.canvas.mode,'select')
        self.ctl.visible.set(True); self.ctl.toggle(); self.pump()
        self.assertIs(self.ctl.result,result)
        self.assertTrue(self.app.canvas.find_withtag('admissibility'))

    def test_unknown_not_green(self):
        settings=deepcopy(self.app.session.check_settings);settings['enabled']=['R.moisture_range']
        self.app.session.set_check_settings(settings);self.app.on_change()
        self.build()
        self.assertTrue(self.app.canvas.find_withtag('map_status:UNKNOWN'))
        self.assertFalse(self.app.canvas.find_withtag('map_status:PASS'))

    def test_cancel_discards_partial(self):
        self.ctl.start(MapOptions('T1',step_m=.35))
        self.ctl.cancel_button.invoke();self.wait()
        self.assertIsNone(self.ctl.result)
        self.assertFalse(self.app.canvas.find_withtag('admissibility'))
        self.assertFalse(self.ctl.running)
        self.assertIn('отменён',self.ctl.status.get())

    def test_json_export_no_session_mutation(self):
        self.build();before=deepcopy(self.app.session.state())
        path=self.folder/'map.json'
        with patch('greencad_editor.admissibility_ui.filedialog.asksaveasfilename',return_value=str(path)):
            self.ctl.save_button.invoke()
        data=json.loads(path.read_text())
        self.assertEqual(data['schema'],'greencad.admissibility-map')
        self.assertEqual(data['point_semantics'],'cell_center')
        self.assertEqual(self.app.session.state(),before)
        project=self.folder/'map.gcp';save_project(self.app.session,project,self.app.canvas.view_state())
        session,view=load_project(project);self.app.adopt(session,view=view);self.pump()
        self.assertEqual(self.ctl.state(),'NOT_BUILT')
        self.assertFalse(self.app.canvas.find_withtag('admissibility'))

    def test_type_choice_fixed_for_map(self):
        self.build()
        self.app.canvas.active_type='T2'
        self.app.select_type(); self.pump()
        self.assertEqual(self.ctl.options.type_id,'T1')
        self.assertEqual(self.ctl.state(),'CURRENT')
        t=deepcopy(self.app.session.types['T1']);t.name+=' modified'
        self.app.session.set_type(t);self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'STALE')

    def test_footer_visible_with_large_font_and_small_dialog(self):
        self.root.tk.call('tk','scaling',2.0)
        self.ctl.choose();dlg=self.ctl.options_window
        for size in ('640x430','800x570'):
            dlg.geometry(size);self.pump()
            for button in (dlg.build_button,dlg.cancel_button):
                self.assertTrue(button.winfo_ismapped())
                bottom=button.winfo_rooty()-dlg.winfo_rooty()+button.winfo_height()
                self.assertLessEqual(bottom,dlg.winfo_height())
                self.assertGreater(button.winfo_width(),1)
        self.click(dlg.cancel_button)
        self.assertFalse(self.app.busy)

if __name__=='__main__':unittest.main()
