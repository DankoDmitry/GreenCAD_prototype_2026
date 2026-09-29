"""Tk canvas adapter. Pixels never enter the domain model or exported coordinates."""
from __future__ import annotations
import math
import tkinter as tk
from typing import Callable
from .models import Point, Feature
from .session import EditorSession

# Display palette; colors do not encode meaning in the input contract.
STYLES = {
    "context.green_zone": ("#BED8B9", "#DDECD9", 10),
    "traffic.road": ("#A8B4BD", "#CCD3D9", 5),
    "traffic.sidewalk": ("#C8C4B8", "#EEECE3", 8),
    "traffic.curb": ("#697773", "", 30),
    "context.project_domain": ("#6F8D7B", "", 40),
    "net.gas": ("#C68520", "", 50),
    "base.building": ("#AD9681", "#E7DACE", 20),
    "dendro.tree": ("#7F937B", "", 25),
    "context.exclusion": ("#B9756E", "#F0DCD6", 15),
}


class MapCanvas(tk.Canvas):
    def __init__(self, master, *, on_change: Callable, on_select: Callable, on_coordinates: Callable, **kwargs):
        super().__init__(master, background="#F7F9F6", highlightthickness=0, **kwargs)
        self.session: EditorSession | None = None
        self.overlay_renderer = None
        self.preview_renderer = None
        self.on_inspect = None
        self.on_change, self.on_select, self.on_coordinates = on_change, on_select, on_coordinates
        self.validation_statuses = {}  # Display only: filled by the report controller.
        self.scale_px = 7.0
        self.center_x, self.center_y = 40.0, 30.0
        self.mode = "select"
        self.active_type = "T1"
        self.snap_m = 0.0
        self.show_grid = True
        self.show_annotations = False
        self.show_labels = True
        self.hidden_layers: set[str] = set()
        self.editable = True
        self.selected_feature: str | None = None
        self._pending = None
        self._drag = None
        self._pan_last = None
        self._fit_on_resize = False
        self.bind("<Configure>", lambda e: self.request_render())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self.zoom(1.15, e.x, e.y))
        self.bind("<Button-5>", lambda e: self.zoom(1/1.15, e.x, e.y))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<ButtonPress-2>", self._pan_press)
        self.bind("<B2-Motion>", self._pan_motion)
        self.bind("<ButtonRelease-2>", self._pan_release)
        self.bind("<ButtonPress-3>", self._pan_press)
        self.bind("<B3-Motion>", self._pan_motion)
        self.bind("<ButtonRelease-3>", self._pan_release)
        self.bind("<Motion>", self._coordinates)
        self.bind("<Escape>", self.cancel_drag)

    def set_session(self, session: EditorSession) -> None:
        self.cancel_drag()
        self.session = session
        self.selected_feature = None
        self.hidden_layers = {d['name'] for d in session.drawing.layers if not d['visible']}
        self.fit()

    def to_screen(self, x: float, y: float) -> Point:
        return ((x-self.center_x)*self.scale_px+self.winfo_width()/2,
                (self.center_y-y)*self.scale_px+self.winfo_height()/2)

    def to_world(self, x: float, y: float) -> Point:
        return (self.center_x+(x-self.winfo_width()/2)/self.scale_px,
                self.center_y-(y-self.winfo_height()/2)/self.scale_px)

    def fit(self) -> None:
        if self.session is None:
            return
        x0,y0,x1,y1 = self.session.drawing.bounds
        # Include moved/added placements, not just the original bounds.
        for p in self.session.placements.values():
            r = self.session.types[p.type_id].radius_m
            x0,y0,x1,y1 = min(x0,p.x-r),min(y0,p.y-r),max(x1,p.x+r),max(y1,p.y+r)
        w, h = max(self.winfo_width()-80, 100), max(self.winfo_height()-80, 100)
        self.scale_px = max(1e-6,min(w/max(x1-x0,1), h/max(y1-y0,1)))
        self.center_x,self.center_y=(x0+x1)/2,(y0+y1)/2
        self.request_render()

    def view_state(self) -> dict:
        return {"center_x":self.center_x,"center_y":self.center_y,"scale_px":self.scale_px,
                "hidden_layers":sorted(self.hidden_layers),"show_annotations":self.show_annotations,
                "show_labels":self.show_labels,"show_grid":self.show_grid,"snap_m":self.snap_m}

    def restore_view(self, values: dict) -> None:
        for key in ("center_x","center_y","scale_px","snap_m"):
            value=values.get(key)
            if isinstance(value,(int,float)) and math.isfinite(value):
                if key in {"scale_px","snap_m"} and value < 0: continue
                if key=="scale_px" and not 1e-6 <= value <= 10000: continue
                setattr(self,key,value)
        self.hidden_layers=set(values.get("hidden_layers",[]))
        for k in ["show_annotations","show_labels","show_grid"]:
            if isinstance(values.get(k),bool): setattr(self,k,values[k])
        self.request_render()

    def zoom(self, factor: float, px: float | None = None, py: float | None = None):
        if self._drag:
            return "break"
        px=self.winfo_width()/2 if px is None else px
        py=self.winfo_height()/2 if py is None else py
        wx,wy=self.to_world(px,py)
        self.scale_px = max(1e-6,min(10000,self.scale_px*factor))
        self.center_x=wx-(px-self.winfo_width()/2)/self.scale_px
        self.center_y=wy+(py-self.winfo_height()/2)/self.scale_px
        self.request_render()
        return "break"

    def _wheel(self,event):
        return self.zoom(1.15 if event.delta>0 else 1/1.15,event.x,event.y)

    def _coordinates(self,event):
        x,y=self.to_world(event.x,event.y)
        self.on_coordinates(x,y)

    def snap(self,x,y):
        if self.snap_m > 0:
            x,y=round(x/self.snap_m)*self.snap_m,round(y/self.snap_m)*self.snap_m
        return x,y

    def _pick(self,event,prefix):
        candidates=self.find_overlapping(event.x-5,event.y-5,event.x+5,event.y+5)
        for item in reversed(candidates):
            for tag in self.gettags(item):
                if tag.startswith(prefix): return tag[len(prefix):]
        return None

    def _press(self,event):
        self.focus_set()
        if self.session is None or not self.editable: return
        if self.mode=="pan":
            self._pan_press(event);return
        x,y=self.to_world(event.x,event.y)
        if self.mode=="inspect" and self.on_inspect is not None:
            self.on_inspect(x,y);return
        if self.mode=="add":
            x,y=self.snap(x,y)
            p=self.session.add(self.active_type,x,y)
            self.selected_feature=None
            self.on_change();self.on_select(p.id,None)
            self.request_render();return
        pid=self._pick(event,"placement:")
        if pid:
            self.session.selected_id=pid
            self.selected_feature=None
            p=self.session.placements[pid]
            self._drag=None if p.locked else {"id":pid,"start_mouse":(x,y),"old":(p.x,p.y),"new":(p.x,p.y)}
            self.on_select(pid,None)
            self.request_render()
        else:
            self.session.selected_id=None
            self.selected_feature=self._pick(event,"feature:")
            self.on_select(None,self.selected_feature)
            self._pan_press(event)
            self.request_render()

    def _motion(self,event):
        if self._drag:
            x,y=self.to_world(event.x,event.y)
            d=self._drag
            oldx,oldy=d['old']; startx,starty=d['start_mouse']
            px,py=self.snap(oldx+x-startx,oldy+y-starty)
            previous=d['new'];d['new']=(px,py)
            self.move("placement:"+d['id'],(px-previous[0])*self.scale_px,-(py-previous[1])*self.scale_px)
            self.on_coordinates(px,py)
        elif self._pan_last:
            self._pan_motion(event)

    def _release(self,event):
        if self._drag and self.session:
            d=self._drag;self._drag=None
            self.session.move(d['id'],*d['new'])
            self.on_change();self.on_select(d['id'],None)
            self.request_render()
        self._pan_release(event)

    def cancel_drag(self,event=None):
        self._drag=None;self._pan_last=None
        self.request_render()
        return "break"

    def _pan_press(self,event):
        if not self.editable or self._drag: return
        self._pan_last=(event.x,event.y)
        self.config(cursor="fleur")

    def _pan_motion(self,event):
        if self._pan_last:
            lx,ly=self._pan_last
            self.center_x-=(event.x-lx)/self.scale_px
            self.center_y+=(event.y-ly)/self.scale_px
            self._pan_last=(event.x,event.y)
            self.request_render()

    def _pan_release(self,event=None):
        self._pan_last=None
        self.config(cursor="crosshair" if self.mode in {"add", "inspect"} else "arrow")

    def request_render(self):
        if self._pending is None:
            self._pending=self.after(16,self.render)

    def _grid(self):
        w,h=self.winfo_width(),self.winfo_height()
        # A visual grid only. Does not imply candidate discretization or admissibility.
        if self.show_grid:
            desired=55/max(self.scale_px,1e-8)
            decade=10**math.floor(math.log10(desired))
            step=next((k*decade for k in (1,2,5,10) if k*decade>=desired),10*decade)
            x0,y1=self.to_world(0,0);x1,y0=self.to_world(w,h)
            for i in range(math.floor(x0/step),math.ceil(x1/step)+1):
                x,_=self.to_screen(i*step,0)
                self.create_line(x,0,x,h,fill="#E8EDE7",tags=("grid",))
            for i in range(math.floor(y0/step),math.ceil(y1/step)+1):
                _,y=self.to_screen(0,i*step)
                self.create_line(0,y,w,y,fill="#E8EDE7",tags=("grid",))

    def render(self):
        self._pending=None
        self.delete("all")
        self._grid()
        if self.session is None:
            self.create_text(self.winfo_width()/2,self.winfo_height()/2,
                             text="Откройте DXF или нажмите «Демо»",fill="#637368",font=("Segoe UI",18))
            return
        features=sorted(self.session.drawing.features,key=lambda f:STYLES.get(f.type_id,(None,None,20))[2])
        for f in features:
            if f.layer in self.hidden_layers or (f.is_annotation and not self.show_annotations): continue
            self._draw_feature(f)
        if self.overlay_renderer is not None:
            self.overlay_renderer(self)
        for p in self.session.placements.values():
            self._draw_placement(p)
        if self.preview_renderer is not None:
            self.preview_renderer(self)
        self._overlays()

    def _draw_feature(self,f:Feature):
        stroke,fill,_=STYLES.get(f.type_id,(None,"",20))
        selected=f.id==self.selected_feature
        tags=("source","feature:"+f.id,"layer:"+f.layer)
        w,h=self.winfo_width(),self.winfo_height()
        for p in f.primitives:
            color="#247487" if selected else (stroke or p.color or "#6A747B")
            width=2.3 if selected else (2.0 if f.type_id=="net.gas" else 1.2)
            if p.kind=="text" and not self.show_annotations: continue
            b=p.bounds();sx0,sy1=self.to_screen(b[0],b[1]);sx1,sy0=self.to_screen(b[2],b[3])
            if p.kind!="text" and (sx1 < -20 or sx0 > w+20 or sy1 < -20 or sy0 > h+20): continue
            if p.kind=="path":
                for path in p.paths:
                    coords=[v for xy in path for v in self.to_screen(*xy)]
                    if len(coords)<4: continue
                    if p.closed and len(path)>=3:
                        self.create_polygon(coords,fill=(fill or "#E8EBE8") if p.fill else "",outline=color,
                                            width=width,tags=tags)
                    else:
                        if p.closed: coords+=coords[:2]
                        self.create_line(coords,fill=color,width=width,
                                         dash=(9,4) if f.type_id=="net.gas" else (),tags=tags)
            elif p.kind=="circle":
                x,y=self.to_screen(*p.center);r=p.radius*self.scale_px
                self.create_oval(x-r,y-r,x+r,y+r,outline=color,width=width,fill=fill if p.fill else "",tags=tags)
            elif p.kind=="point":
                x,y=self.to_screen(*p.center)
                self.create_line(x-3,y,x+3,y,fill=color,tags=tags)
                self.create_line(x,y-3,x,y+3,fill=color,tags=tags)
            elif p.kind=="text":
                x,y=self.to_screen(*p.center)
                size=max(6,min(32,int(p.text_height*self.scale_px)))
                self.create_text(x,y,text=p.text,anchor="sw",fill=color,font=("Segoe UI",-size),angle=p.angle,tags=tags+("annotation",))

    def _draw_placement(self,p):
        t=self.session.types[p.type_id]
        pos=self._drag['new'] if self._drag and self._drag['id']==p.id else (p.x,p.y)
        x,y=self.to_screen(*pos)
        r=t.radius_m*self.scale_px
        visual_r=max(5,r)
        selected=self.session.selected_id==p.id
        tags=("placements","placement:"+p.id)
        if t.symbol=="circle":
            self.create_oval(x-r,y-r,x+r,y+r,fill=t.color,stipple="gray50",outline=t.color,width=2,tags=tags)
        else:
            self.create_polygon(x,y-6,x+6,y,x,y+6,x-6,y,fill=t.color,outline=t.color,tags=tags)
        self.create_oval(x-2.5,y-2.5,x+2.5,y+2.5,fill=t.color,outline="",tags=tags)
        if selected:
            self.create_oval(x-visual_r-4,y-visual_r-4,x+visual_r+4,y+visual_r+4,outline="#20647E",dash=(4,3),width=2,tags=tags)
            self.create_line(x-7,y,x+7,y,fill="#124858",tags=tags)
            self.create_line(x,y-7,x,y+7,fill="#124858",tags=tags)
        status = self.validation_statuses.get(p.id)
        if self._drag and self._drag['id'] == p.id:
            status = None  # Never carry a green check to an unverified preview position.
        if status:
            colors = {'PASS':'#28784B', 'FAIL':'#B43B35', 'UNKNOWN':'#BC8A20',
                      'ERROR':'#8052A4', 'WARNING':'#BC8A20', 'NOT_CHECKED':'#7D8881'}
            self.create_oval(x-visual_r-7,y-visual_r-7,x+visual_r+7,y+visual_r+7,
                             outline=colors.get(status,'#7D8881'),width=2,
                             tags=(*tags,'validation:'+p.id))
        if p.locked:
            self.create_rectangle(x-3,y-3,x+3,y+3,fill="#465454",outline="white",tags=tags)
        if self.show_labels:
            label=p.label or t.id
            self.create_text(x,y-visual_r-9,text=label,fill="#2C453A",font=("Segoe UI",9,"bold"),tags=tags)

    def _overlays(self):
        w,h=self.winfo_width(),self.winfo_height()
        # Orientation and an adaptive scale bar; not a georeferencing claim.
        self.create_line(w-30,52,w-30,23,arrow="last",fill="#597263",width=2)
        self.create_text(w-30,12,text="+Y",fill="#597263",font=("Segoe UI",8))
        desired=100/self.scale_px
        decade=10**math.floor(math.log10(desired))
        length=max(k*decade for k in (1,2,5,10) if k*decade<=desired)
        px=length*self.scale_px
        self.create_rectangle(17,h-48,45+px,h-10,fill="#F7F9F6",outline="")
        self.create_line(26,h-23,26+px,h-23,fill="#405748",width=2)
        self.create_line(26,h-27,26,h-19,fill="#405748")
        self.create_line(26+px,h-27,26+px,h-19,fill="#405748")
        self.create_text(26+px/2,h-36,text=f"{length:g} м",fill="#405748",font=("Segoe UI",9))
