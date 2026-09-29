"""Synthetic test scene, not a real project and not a normative planting plan."""
from __future__ import annotations
from pathlib import Path
import ezdxf
from .metadata import encode_metadata
from .catalog import default_mapping


def make_demo(path: str | Path, *, millimetres: bool = False, origin=(0.0, 0.0)) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM if millimetres else ezdxf.units.M
    factor = 1000 if millimetres else 1
    msp = doc.modelspace()
    mapping = default_mapping()
    colors = {"GC_ROAD_AREA":0xC2CBD1,"GC_SIDEWALK_AREA":0xE6E3D8,
              "GC_GREEN_ZONE":0xD1E6CE,"GC_SITE_BOUNDARY":0x6B8576,
              "GC_CURB_EDGE":0x7A817D,"GC_GAS_AXIS":0xC58520}
    for name, color in colors.items():
        doc.layers.new(name, dxfattribs={"true_color":color, "color":7})
    def point(x,y):
        return ((x+origin[0])*factor, (y+origin[1])*factor)
    def asset(e, identifier, layer, **props):
        encode_metadata(e, {"role":"asset", "id":identifier,
                            **mapping[layer], "properties":{"synthetic":True, **props}})
        return e
    def rectangle(identifier, layer, x0,y0,x1,y1):
        e=msp.add_lwpolyline([point(x0,y0),point(x1,y0),point(x1,y1),point(x0,y1)], close=True, dxfattribs={"layer":layer})
        return asset(e,identifier,layer)
    rectangle("site_01","GC_SITE_BOUNDARY",0,0,80,60)
    rectangle("green_01","GC_GREEN_ZONE",2,2,78,58)
    rectangle("road_w","GC_ROAD_AREA",-8,-8,0,68)
    rectangle("road_e","GC_ROAD_AREA",80,-8,88,68)
    rectangle("road_s","GC_ROAD_AREA",0,-8,80,0)
    rectangle("road_n","GC_ROAD_AREA",0,60,80,68)
    rectangle("walk_w","GC_SIDEWALK_AREA",0,0,2,60)
    rectangle("walk_e","GC_SIDEWALK_AREA",78,0,80,60)
    rectangle("walk_s","GC_SIDEWALK_AREA",2,0,78,2)
    rectangle("walk_n","GC_SIDEWALK_AREA",2,58,78,60)
    # Each curb is an edge, not a filled footprint or an inferred protection zone.
    for n, a, b in [("s",(0,0),(80,0)),("e",(80,0),(80,60)),("n",(80,60),(0,60)),("w",(0,60),(0,0))]:
        layer="GC_CURB_EDGE"
        asset(msp.add_line(point(*a),point(*b),dxfattribs={"layer":layer}),"curb_"+n,layer)
    layer="GC_GAS_AXIS"
    asset(msp.add_lwpolyline([point(-8,30),point(40,30),point(88,30)],dxfattribs={"layer":layer}),
          "gas_01",layer, pressure_category=None, depth_m=None, A01=None,
          note="Условная ось газопровода; охранная зона и отступы в этом редакторе не вычисляются.")
    doc.set_modelspace_vport(90*factor, center=point(40,30))
    doc.saveas(path)
    return path
