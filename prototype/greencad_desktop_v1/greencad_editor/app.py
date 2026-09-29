from __future__ import annotations
import argparse
from pathlib import Path
import sys
import logging
from logging.handlers import RotatingFileHandler
from .catalog import ROOT


def main(argv=None):
    parser=argparse.ArgumentParser(description='GreenCAD — ручной 2D DXF-редактор с проверкой выбранных условий')
    parser.add_argument('path',nargs='?',help='DXF или проект .gcp')
    parser.add_argument('--empty',action='store_true',help='Не открывать демонстрационный DXF при старте')
    parser.add_argument('--inspect',action='store_true',help='Показать сведения о DXF без графического окна')
    args=parser.parse_args(argv)
    if args.inspect:
        import json
        from .dxf_io import load_dxf
        d=load_dxf(args.path or ROOT/'examples'/'demo_block.dxf')
        print(json.dumps({'file':d.source_name,'units_m':d.metres_per_unit,'features':len(d.features),
                          'placements':len(d.placements),'bounds':d.bounds,'diagnostics':d.diagnostics},ensure_ascii=False,indent=2))
        return 0
    logs=ROOT/'logs'
    try:
        logs.mkdir(exist_ok=True)
        handler=RotatingFileHandler(logs/'editor.log',maxBytes=2_000_000,backupCount=2,encoding='utf-8')
        logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    except OSError:
        logging.basicConfig(level=logging.INFO)
    try:
        import tkinter as tk
        from .ui import EditorApp
        if sys.platform=='win32':
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:pass
        root=tk.Tk();app=EditorApp(root)
        path=Path(args.path) if args.path else (None if args.empty else ROOT/'examples'/'demo_block.dxf')
        if path:root.after(80,lambda:app.open_path(path,confirm=False))
        root.mainloop()
        return 0
    except Exception:
        logging.exception('Приложение не запущено')
        raise

if __name__=='__main__':
    raise SystemExit(main())
