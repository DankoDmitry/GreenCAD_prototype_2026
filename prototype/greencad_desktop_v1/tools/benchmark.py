"""Synthetic rendering benchmark. Does not generate or validate a planting plan."""
from pathlib import Path
from dataclasses import asdict
import importlib.metadata
import json,platform,sys,time,statistics,tempfile,tkinter as tk
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.dxf_io import load_dxf,export_dxf
from greencad_editor.session import EditorSession
from greencad_editor.models import Placement
from greencad_editor.ui import EditorApp

root_dir=Path(__file__).resolve().parents[1]
start=time.perf_counter();d=load_dxf(root_dir/'examples/demo_block.dxf');load_ms=(time.perf_counter()-start)*1000
root=tk.Tk();app=EditorApp(root);root.update();s=EditorSession(d)
app.adopt(s)
for _ in range(5):root.update();time.sleep(.03)
results=[]
for count in (100,1000):
    state=s.state()
    state['placements']=[asdict(Placement(f'bench-{i}','T4',3+(i%50)*1.45,3+(i//50)*2.5)) for i in range(count)]
    s.restore(state,mark_saved=True);app.on_change();root.update()
    times=[]
    for i in range(7):
        start=time.perf_counter();app.canvas.render();root.update_idletasks();times.append((time.perf_counter()-start)*1000)
    with tempfile.TemporaryDirectory() as tmp:
        start=time.perf_counter();report=export_dxf(s.drawing,list(s.placements.values()),s.types,Path(tmp)/'test.dxf')
        export_ms=(time.perf_counter()-start)*1000
    results.append({'synthetic_markers':count,'full_canvas_redraw_ms_median':round(statistics.median(times),2),'full_canvas_redraw_ms_max':round(max(times),2),
                    'export_with_reread_verification_ms':round(export_ms,2),'export_passed':report['status']=='PASS'})
rss=None
try:
    import psutil
    rss=round(psutil.Process().memory_info().rss/1024**2,1)
except ImportError:pass
out={'environment':{'python':sys.version,'platform':platform.platform(),'tk':tk.TkVersion,'ezdxf':importlib.metadata.version('ezdxf'),
                    'canvas_px':[app.canvas.winfo_width(),app.canvas.winfo_height()]},
     'demo_load_ms':round(load_ms,2),'process_rss_mib_at_end':rss,'results':results,
     'limits':'Synthetic 15-entity source, virtual Linux display. Not a benchmark of large real DXF or Windows performance.'}
(root_dir/'test_results/benchmark.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(out,ensure_ascii=False,indent=2))
app.executor.shutdown(wait=True);root.destroy()
