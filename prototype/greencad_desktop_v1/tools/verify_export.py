from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.dxf_io import read_document,verify_source_preserved
p=argparse.ArgumentParser(description='Compare source graphical entities/layers in two DXF documents')
p.add_argument('source');p.add_argument('result')
a=p.parse_args()
r=verify_source_preserved(read_document(Path(a.source).read_bytes()),read_document(Path(a.result).read_bytes()))
print(json.dumps(r,ensure_ascii=False,indent=2))
raise SystemExit(0 if r['passed'] else 1)
