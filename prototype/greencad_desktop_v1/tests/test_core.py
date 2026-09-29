from __future__ import annotations
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile
import ezdxf
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf, load_bytes, export_dxf, read_document, verify_source_preserved, sha256
from greencad_editor.session import EditorSession
from greencad_editor.project_io import save_project, load_project
from greencad_editor.metadata import encode_metadata, decode_metadata, APPID
from greencad_editor.errors import EditorError, UnitsRequired
from greencad_editor.models import ProjectType, Placement
from greencad_editor.catalog import load_plants, load_object_types


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.path=make_demo(self.root/'Участок с пробелами.dxf')
        self.d=load_dxf(self.path);self.s=EditorSession(self.d)

    def tearDown(self):self.tmp.cleanup()

    def test_demo_contract(self):
        self.assertEqual(len(self.d.features),15)
        self.assertEqual(self.d.metres_per_unit,1)
        self.assertEqual(self.d.bounds,(-8,-8,88,68))
        self.assertFalse(self.d.diagnostics)
        self.assertEqual(sum(f.type_id=='traffic.road' for f in self.d.features),4)
        self.assertEqual(sum(f.type_id=='net.gas' for f in self.d.features),1)
        self.assertEqual(sum(f.type_id=='context.green_zone' for f in self.d.features),1)
        self.assertTrue(all(f.classification_source=='XDATA' for f in self.d.features))

    def test_catalog_indices(self):
        plants=load_plants();types=load_object_types()
        self.assertEqual(len(plants),329);self.assertEqual(len({x['id'] for x in plants}),329)
        self.assertEqual(len(types),140)
        self.assertIn('net.gas',types)
        self.assertTrue(all(t.plant_id is None for t in self.s.types.values()))

    def test_round_trip_source_preserved(self):
        raw=self.path.read_bytes()
        p=self.s.add('T1',12.125,45.875)
        self.s.change(p.id,plant_id=load_plants()[0]['id'],root_protection=True,label='Дерево №1')
        self.s.add('T5',50,20)
        target=self.root/'Новый вариант.dxf'
        report=export_dxf(self.d,list(self.s.placements.values()),self.s.types,target)
        self.assertEqual(self.path.read_bytes(),raw)
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['source_preservation']['changed'],[])
        self.assertEqual(report['normative_validation'],'NOT_IMPLEMENTED')
        new=load_dxf(target)
        self.assertEqual(len(new.placements),2)
        q=next(x for x in new.placements if x.id==p.id)
        self.assertEqual((q.x,q.y),(12.125,45.875))
        self.assertTrue(q.root_protection);self.assertEqual(q.label,'Дерево №1')
        self.assertTrue(verify_source_preserved(read_document(raw),read_document(target.read_bytes()))['passed'])
        self.assertFalse(ezdxf.readfile(target).audit().has_errors)

    def test_repeated_export_no_duplicates(self):
        p=self.s.add('T2',18,19)
        a=self.root/'a.dxf';b=self.root/'b.dxf'
        export_dxf(self.d,list(self.s.placements.values()),self.s.types,a)
        session=EditorSession(load_dxf(a));session.move(p.id,55,44)
        export_dxf(session.drawing,list(session.placements.values()),session.types,b)
        result=load_dxf(b)
        self.assertEqual(len(result.placements),1)
        self.assertEqual((result.placements[0].x,result.placements[0].y),(55,44))
        self.assertEqual(len(ezdxf.readfile(b).modelspace()),16)
        session.delete(p.id)
        export_dxf(session.drawing,list(session.placements.values()),session.types,self.root/'c.dxf')
        self.assertEqual(len(load_dxf(self.root/'c.dxf').placements),0)

    def test_no_overwrite_source_or_its_copy(self):
        with self.assertRaises(EditorError):export_dxf(self.d,[],self.s.types,self.path)
        cp=self.root/'copy.dxf';cp.write_bytes(self.path.read_bytes())
        with self.assertRaises(EditorError):export_dxf(self.d,[],self.s.types,cp)

    def test_units_mm_and_large_origin(self):
        path=make_demo(self.root/'millimetres.dxf',millimetres=True,origin=(1230000,-870000))
        d=load_dxf(path);s=EditorSession(d)
        self.assertEqual(d.metres_per_unit,.001)
        s.add('T1',1230020.25,-869968.75)
        out=self.root/'millimetres_result.dxf'
        export_dxf(d,list(s.placements.values()),s.types,out)
        d2=load_dxf(out);p=d2.placements[0]
        self.assertAlmostEqual(p.x,1230020.25,places=8);self.assertAlmostEqual(p.y,-869968.75,places=8)
        e=list(ezdxf.readfile(out).modelspace())[-1]
        self.assertAlmostEqual(e.dxf.center.x,1230020250.)
        self.assertAlmostEqual(e.dxf.radius,3000.)

    def test_unitless_requires_explicit_scale(self):
        doc=ezdxf.new('R2010');doc.units=0;doc.modelspace().add_line((0,0),(1000,0))
        path=self.root/'unitless.dxf';doc.saveas(path)
        with self.assertRaises(UnitsRequired):load_dxf(path)
        d=load_dxf(path,metres_per_unit=.001)
        self.assertEqual(d.features[0].primitives[0].paths[0][-1],(1,0))
        with self.assertRaises(EditorError):load_dxf(path,metres_per_unit=0)

    def test_unitless_scale_persists_in_export_profile(self):
        doc=ezdxf.new('R2010');doc.units=0;doc.modelspace().add_line((0,0),(1000,0))
        path=self.root/'unitless_source.dxf';doc.saveas(path)
        d=load_dxf(path,metres_per_unit=.001);s=EditorSession(d);s.add('T1',1.25,2.5)
        out=self.root/'unitless_result.dxf';export_dxf(d,list(s.placements.values()),s.types,out)
        r=load_dxf(out)
        self.assertEqual(r.metres_per_unit,.001)
        self.assertEqual((r.placements[0].x,r.placements[0].y),(1.25,2.5))
        self.assertEqual(ezdxf.readfile(out).units,0)

    def test_known_units_cannot_be_silently_overridden(self):
        with self.assertRaises(EditorError):load_dxf(self.path,metres_per_unit=.001)

    def test_portable_project(self):
        p=self.s.add('T1',10,20)
        self.s.change(p.id,root_protection=True,locked=True,properties={'A01':None,'future':{'flag':True}})
        path=self.root/'test.gcp'
        save_project(self.s,path,{'center_x':25,'scale_px':8})
        self.assertFalse(self.s.dirty)
        self.path.unlink() # Original file not needed after saving a portable project.
        new,view=load_project(path)
        self.assertEqual(new.state(),self.s.state())
        self.assertEqual(view['center_x'],25)
        self.assertEqual(new.drawing.source_bytes,self.d.source_bytes)
        export_dxf(new.drawing,list(new.placements.values()),new.types,self.root/'from_project.dxf')

    def test_project_rejects_damage_and_future_version(self):
        state=self.s.state()
        for version,hashval in [(99,self.d.source_sha256),(1,'wrong')]:
            f=self.root/f'bad{version}.gcp'
            with zipfile.ZipFile(f,'w') as z:
                z.writestr('source.dxf',self.d.source_bytes)
                z.writestr('project.json',json.dumps({'schema':'greencad.desktop-project','schema_version':version,
                    'source':{'sha256':hashval,'name':'a.dxf','metres_per_unit':1},'state':state}))
            with self.assertRaises(EditorError):load_project(f)

    def test_project_rejects_arbitrary_zip_contents(self):
        p=self.root/'bad.gcp'
        with zipfile.ZipFile(p,'w') as z:z.writestr('../outside.txt','no')
        with self.assertRaises(EditorError):load_project(p)
        self.assertFalse((self.root.parent/'outside.txt').exists())

    def test_undo_redo_delete_copy_and_lock(self):
        p=self.s.add('T1',5,6)
        self.s.move(p.id,20,30);self.assertTrue(self.s.undo())
        self.assertEqual(self.s.placements[p.id].x,5)
        self.s.redo();self.assertEqual(self.s.placements[p.id].x,20)
        self.s.change(p.id,locked=True)
        self.s.move(p.id,99,99);self.assertEqual(self.s.placements[p.id].x,20)
        with self.assertRaises(EditorError):self.s.change(p.id,x=100)
        q=self.s.duplicate(p.id);self.assertFalse(q.locked)
        self.s.delete(p.id);self.s.undo();self.assertIn(p.id,self.s.placements)

    def test_type_level_replacement(self):
        p=self.s.add('T1',10,20)
        self.s.change(p.id,plant_id=load_plants()[0]['id'])
        spec=ProjectType('T1','Новый общий тип',radius_m=1.5,color='#AB00CD',plant_id=load_plants()[1]['id'])
        self.s.set_type(spec,clear_overrides=True)
        self.assertIsNone(self.s.placements[p.id].plant_id)
        self.assertEqual(self.s.types['T1'].plant_id,load_plants()[1]['id'])
        self.s.undo();self.assertEqual(self.s.types['T1'].radius_m,3.)

    def test_no_rule_application_even_outside_zone(self):
        p=self.s.add('T1',-999,999)
        self.assertEqual(p.x,-999)
        self.assertEqual(len(self.s.placements),1)

    def test_nonfinite_values_rejected_without_change(self):
        p=self.s.add('T1',10,20)
        for value in [float('nan'),float('inf'),True,'oops']:
            with self.assertRaises(EditorError):self.s.move(p.id,value,20)
        self.assertEqual(self.s.placements[p.id].x,10)
        with self.assertRaises(EditorError):ProjectType.from_dict({'id':'T','name':'test','radius_m':-1})

    def test_text_never_establishes_semantics(self):
        doc=ezdxf.new('R2010');doc.units=6
        doc.layers.new('GC_GAS_AXIS')
        doc.modelspace().add_text('Это газопровод или бордюр',dxfattribs={'layer':'GC_GAS_AXIS','height':2})
        doc.modelspace().add_line((0,0),(10,0),dxfattribs={'layer':'0'})
        path=self.root/'text.dxf';doc.saveas(path)
        d=load_dxf(path)
        self.assertEqual(d.features[0].classification_source,'ANNOTATION')
        self.assertIsNone(d.features[0].type_id)
        self.assertIsNone(d.features[1].type_id)
        s=EditorSession(d);s.set_mapping({'0':{'type_id':'net.gas','geometry_role':'AXIS'}})
        self.assertEqual(d.features[1].type_id,'net.gas')
        out=self.root/'mapping_export.dxf';export_dxf(d,[],s.types,out)
        r=load_dxf(out)
        self.assertEqual(r.features[1].type_id,'net.gas')
        self.assertEqual(r.features[0].classification_source,'ANNOTATION')

    def test_malformed_xdata_and_duplicate_ids_rejected(self):
        doc=ezdxf.readfile(self.path)
        e=doc.modelspace().add_point((0,0));e.set_xdata(APPID,[(1000,'{')])
        p=self.root/'broken.dxf';doc.saveas(p)
        with self.assertRaises(EditorError):load_dxf(p)
        doc=ezdxf.readfile(self.path)
        original=next(iter(doc.modelspace()));copy=original.copy();doc.modelspace().add_entity(copy)
        p=self.root/'duplicate.dxf';doc.saveas(p)
        with self.assertRaises(EditorError):load_dxf(p)

    def test_metadata_unicode_chunking(self):
        doc=ezdxf.new('R2010');e=doc.modelspace().add_point((0,0))
        meta={'role':'asset','id':'Русское имя','type_id':'net.gas','geometry_role':'AXIS','properties':{'note':'Проверка Unicode '*60}}
        encode_metadata(e,meta);result=decode_metadata(e)
        self.assertEqual(result['properties'],meta['properties'])
        self.assertTrue(all(len(t.value.encode('ascii'))<=200 for t in e.get_xdata(APPID)))

    def test_external_cad_move_uses_geometry_not_stale_metadata(self):
        p=self.s.add('T1',10,20);out=self.root/'one.dxf'
        export_dxf(self.d,list(self.s.placements.values()),self.s.types,out)
        doc=ezdxf.readfile(out)
        for e in doc.modelspace():
            meta=decode_metadata(e)
            if meta and meta.get('role')=='placement':e.dxf.center=(44,33,0)
        doc.saveas(out)
        r=load_dxf(out);self.assertEqual((r.placements[0].x,r.placements[0].y),(44,33))

    def test_blocks_hatches_curves_and_annotations_preserved(self):
        doc=ezdxf.new('R2010');doc.units=6
        m=doc.modelspace()
        b=doc.blocks.new('A');b.add_circle((0,0),2);b.add_line((0,0),(1,1))
        m.add_blockref('A',(100,100),dxfattribs={'rotation':30,'xscale':2,'yscale':2})
        m.add_arc((20,20),5,10,200)
        m.add_ellipse((30,30),major_axis=(4,0),ratio=.5)
        m.add_spline([(0,0),(10,20),(20,0)])
        m.add_lwpolyline([(0,0,0,0,1),(10,0,0,0,0)],format='xyseb')
        h=m.add_hatch();h.paths.add_polyline_path([(40,0),(60,0),(60,20),(40,20)],is_closed=True)
        h.paths.add_polyline_path([(45,5),(55,5),(55,15),(45,15)],is_closed=True,flags=0)
        m.add_mtext('Не интерпретировать эту подпись',dxfattribs={'insert':(0,30),'char_height':1})
        m.add_point((1,2,8))
        doc.layout().add_line((1,1),(5,6))
        path=self.root/'entities.dxf';doc.saveas(path)
        d=load_dxf(path);s=EditorSession(d)
        self.assertTrue(d.features[0].primitives)
        self.assertIn('hatch_outlines',{x['code'] for x in d.diagnostics})
        self.assertIn('xy_projection',{x['code'] for x in d.diagnostics})
        s.add('T1',15,15)
        out=self.root/'entities_out.dxf';rep=export_dxf(d,list(s.placements.values()),s.types,out)
        self.assertTrue(rep['source_preservation']['passed'])

    def test_export_retains_unmanaged_entity_on_result_named_layer(self):
        doc=ezdxf.readfile(self.path);doc.layers.new('GC_RESULT_unmanaged')
        doc.modelspace().add_circle((5,5),1,dxfattribs={'layer':'GC_RESULT_unmanaged'})
        doc.saveas(self.path)
        d=load_dxf(self.path);s=EditorSession(d)
        out=self.root/'retained.dxf';export_dxf(d,[],s.types,out)
        self.assertEqual(len(ezdxf.readfile(out).modelspace().query('*[layer=="GC_RESULT_unmanaged"]')),1)

    def test_xref_is_not_followed(self):
        doc=ezdxf.new('R2010');doc.units=6
        doc.add_xref_def(filename='not_available.dxf',name='missing')
        doc.modelspace().add_blockref('missing',(0,0))
        path=self.root/'xref.dxf';doc.saveas(path)
        d=load_dxf(path)
        self.assertIn('unresolved_xref',{x['code'] for x in d.diagnostics})

    def test_export_failure_keeps_old_target(self):
        path=self.root/'already_exists.dxf';path.write_bytes(b'keep me')
        p=self.s.add('T1',1,2);self.s.placements[p.id].properties={'note':'x'*15000}
        with self.assertRaises(EditorError):export_dxf(self.d,list(self.s.placements.values()),self.s.types,path)
        self.assertEqual(path.read_bytes(),b'keep me')


if __name__=='__main__':unittest.main()
