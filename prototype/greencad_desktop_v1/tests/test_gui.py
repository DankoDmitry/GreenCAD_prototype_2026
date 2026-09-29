"""Tests against actual Tk widgets and generated mouse events, not a mock canvas.
Linux headless: xvfb-run -a python -m unittest discover -s tests -p 'test_gui.py' -v
"""
from __future__ import annotations
import os
import sys
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import tkinter as tk
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.session import EditorSession
from greencad_editor.ui import EditorApp, PlantPicker, TypeDialog, MappingDialog

HAS_DISPLAY = sys.platform in ('win32','darwin') or bool(os.environ.get('DISPLAY'))

@unittest.skipUnless(HAS_DISPLAY, 'Требуется графическая сессия или Xvfb')
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.path=make_demo(self.folder/'input.dxf')
        self.root=tk.Tk();self.app=EditorApp(self.root)
        self.errors=[]
        self.error_patch=patch('greencad_editor.ui.messagebox.showerror',side_effect=lambda *a,**k:self.errors.append(a))
        self.error_patch.start()
        self.root.report_callback_exception=lambda typ,value,tb:self.errors.append(value)
        self.root.update()
        self.app.adopt(EditorSession(load_dxf(self.path)))
        self.pump()

    def tearDown(self):
        self.app.executor.shutdown(wait=True)
        self.root.destroy();self.tmp.cleanup()
        self.error_patch.stop()
        errors=self.errors
        self.app=None;self.root=None
        import gc
        gc.collect()
        self.assertFalse(errors,repr(errors))

    def pump(self,n=7):
        for _ in range(n):self.root.update();time.sleep(.01)

    def wait_task(self):
        start=time.perf_counter()
        while self.app.busy and time.perf_counter()-start<10:self.pump(2)
        self.assertFalse(self.app.busy)
        self.pump()

    def click_world(self,x,y):
        c=self.app.canvas;sx,sy=c.to_screen(x,y)
        c.event_generate('<ButtonPress-1>',x=round(sx),y=round(sy));self.pump(2)
        c.event_generate('<ButtonRelease-1>',x=round(sx),y=round(sy));self.pump()

    def add(self,x=20,y=20):
        self.app.mode.set('add');self.app.mode_changed();self.click_world(x,y)
        self.app.mode.set('select');self.app.mode_changed();self.pump()
        return self.app.session.placements[self.app.session.selected_id]

    def test_add_drag_undo_redo_delete(self):
        p=self.add();c=self.app.canvas
        old=(p.x,p.y);sx,sy=c.to_screen(*old);sx,sy=round(sx),round(sy)
        scale=c.scale_px
        c.event_generate('<ButtonPress-1>',x=sx,y=sy);self.pump()
        c.event_generate('<B1-Motion>',x=sx+65,y=sy-32);self.pump()
        # Preview does not mutate model before mouse release.
        self.assertEqual((self.app.session.placements[p.id].x,self.app.session.placements[p.id].y),old)
        c.event_generate('<ButtonRelease-1>',x=sx+65,y=sy-32);self.pump()
        new=self.app.session.placements[p.id]
        self.assertAlmostEqual(new.x,old[0]+65/scale,places=7)
        self.assertAlmostEqual(new.y,old[1]+32/scale,places=7)
        self.app.undo();self.pump();self.assertAlmostEqual(self.app.session.placements[p.id].x,old[0])
        self.app.redo();self.pump();self.assertAlmostEqual(self.app.session.placements[p.id].x,old[0]+65/scale)
        self.app.delete();self.pump();self.assertEqual(len(self.app.session.placements),0)
        self.app.undo();self.pump();self.assertEqual(len(self.app.session.placements),1)

    def test_zoom_pan_coordinate_consistency(self):
        c=self.app.canvas
        before=c.to_world(230,220)
        c.zoom(2,230,220);self.pump()
        after=c.to_world(230,220)
        self.assertAlmostEqual(before[0],after[0]);self.assertAlmostEqual(before[1],after[1])
        center=(c.center_x,c.center_y);scale=c.scale_px
        c.event_generate('<ButtonPress-3>',x=200,y=200)
        c.event_generate('<B3-Motion>',x=240,y=230)
        c.event_generate('<ButtonRelease-3>',x=240,y=230);self.pump()
        self.assertAlmostEqual(c.center_x,center[0]-40/scale)
        self.assertAlmostEqual(c.center_y,center[1]+30/scale)
        w=(17.125,36.875);self.assertAlmostEqual(c.to_world(*c.to_screen(*w))[0],w[0])

    def test_snap_and_properties(self):
        self.app.snap.set('1 м');self.app.view_options()
        p=self.add(10.2,11.25)
        self.assertEqual((p.x,p.y),(10,11))
        self.app.edit_type_combo.current(2)
        self.app.x_var.set('22,125');self.app.y_var.set('18.875');self.app.label_var.set('Куст №7')
        self.app.root_var.set(True);self.app._plant_override=self.app.plants[0]['id']
        self.app.apply_properties();self.pump()
        p=self.app.session.placements[p.id]
        self.assertEqual((p.x,p.y),(22.125,18.875));self.assertEqual(p.type_id,'T3')
        self.assertEqual(p.label,'Куст №7');self.assertTrue(p.root_protection)

    def test_select_source_and_toggle_layer(self):
        self.click_world(40,30)
        fid=self.app.canvas.selected_feature
        f=next(x for x in self.app.session.drawing.features if x.id==fid)
        self.assertEqual(f.type_id,'net.gas')
        self.assertIsNone(self.app.session.selected_id)
        self.assertIn('net.gas',self.app.info.get('1.0','end'))
        for iid in self.app.layers_tree.get_children():
            if self.app.layers_tree.item(iid,'values')[1]=='GC_GAS_AXIS':
                self.app.layers_tree.selection_set(iid);break
        self.app.toggle_layer();self.pump()
        self.assertIn('GC_GAS_AXIS',self.app.canvas.hidden_layers)
        self.assertFalse(self.app.canvas.find_withtag('feature:gas_01'))
        self.assertEqual(len(self.app.session.drawing.features),15)

    def test_project_and_export_through_ui(self):
        p=self.add(15,25);pid=p.id
        with patch('greencad_editor.ui.filedialog.asksaveasfilename',return_value=str(self.folder/'work.gcp')):
            self.assertTrue(self.app.save())
        with patch('greencad_editor.ui.filedialog.asksaveasfilename',return_value=str(self.folder/'result.dxf')), patch('greencad_editor.ui.messagebox.showinfo'):
            self.app.export();self.wait_task()
        result=load_dxf(self.folder/'result.dxf')
        self.assertEqual(len(result.placements),1);self.assertEqual(result.placements[0].id,pid)
        self.assertTrue((self.folder/'result.export.json').exists())
        self.app.open_path(self.folder/'work.gcp',confirm=False);self.wait_task()
        self.assertIn(pid,self.app.session.placements)
        self.assertFalse(self.app.session.dirty)

    def test_type_and_plant_dialogs(self):
        spec=self.app.session.types['T1']
        def done(t,clear):self.app.session.set_type(t,clear_overrides=clear)
        dlg=TypeDialog(self.root,spec,self.app.plants,done);self.pump()
        dlg.name.set('Тестовый тип');dlg.radius.set('2.25');dlg.accept();self.pump()
        self.assertEqual(self.app.session.types['T1'].radius_m,2.25)
        chosen=[];picker=PlantPicker(self.root,self.app.plants,chosen.append)
        picker.query.set('Ель колючая');self.pump()
        rows=picker.tree.get_children();self.assertGreater(len(rows),0)
        picker.tree.selection_set(rows[0]);picker.accept();self.pump()
        self.assertEqual(chosen[0],rows[0])

    def test_mapping_dialog_does_not_change_xdata(self):
        dlg=MappingDialog(self.root,self.app.session,self.app.object_types,self.app.session.set_mapping)
        self.pump();dlg.mapping['GC_GAS_AXIS']={'type_id':'traffic.curb','geometry_role':'EDGE'}
        dlg.finish();self.pump()
        gas=next(x for x in self.app.session.drawing.features if x.id=='gas_01')
        self.assertEqual(gas.type_id,'net.gas');self.assertEqual(gas.classification_source,'XDATA')

    def test_empty_selection_and_cancel_drag(self):
        p=self.add();c=self.app.canvas;sx,sy=c.to_screen(p.x,p.y)
        c.event_generate('<ButtonPress-1>',x=round(sx),y=round(sy));self.pump()
        c.event_generate('<B1-Motion>',x=round(sx+50),y=round(sy));self.pump()
        c.cancel_drag();self.pump()
        self.assertEqual((self.app.session.placements[p.id].x,self.app.session.placements[p.id].y),(p.x,p.y))

if __name__=='__main__':unittest.main()
