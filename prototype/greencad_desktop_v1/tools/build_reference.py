"""Build a deterministic, validated working snapshot. No network or eval."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
import json
from greencad_editor.reference_build import build_from_sources, SOURCES
from greencad_editor.reference_data import DEFAULT_PATH, write_bundle, read_bundle
from greencad_editor.errors import EditorError


def main():
    p=argparse.ArgumentParser(description='Собрать общий пакет справочников GreenCAD')
    p.add_argument('--sources',type=Path,default=SOURCES)
    p.add_argument('--registry',type=Path)
    p.add_argument('--site',type=Path)
    p.add_argument('--registry-xlsx',type=Path)
    p.add_argument('--site-xlsx',type=Path)
    p.add_argument('--json-only',action='store_true')
    p.add_argument('--profile',type=Path)
    p.add_argument('--output',type=Path,default=DEFAULT_PATH)
    p.add_argument('--check',action='store_true',help='Сравнить с готовым пакетом, не записывать')
    a=p.parse_args()
    try:
        bundle=build_from_sources(sources_path=a.sources,registry_path=a.registry,site_path=a.site,
             registry_xlsx=a.registry_xlsx,site_xlsx=a.site_xlsx,use_workbooks=not a.json_only,profile_path=a.profile)
        if a.check:
            if read_bundle(a.output).id!=bundle.id:raise EditorError('Пакет отличается от результата сборки.')
        else:write_bundle(bundle,a.output)
        print(json.dumps(bundle.summary(),ensure_ascii=False,indent=2))
        return 0
    except (EditorError,OSError,ValueError) as e:
        print('Сборка отклонена: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
