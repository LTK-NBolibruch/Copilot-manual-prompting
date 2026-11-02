"""Windows 11 virtual desktop integration (pyvda)."""
import ctypes,logging,os,time,threading
log=logging.getLogger("desktops")
def _api():
    if os.name!="nt": raise RuntimeError("Windows virtual desktops require Windows.")
    try:
        from pyvda import AppView,VirtualDesktop,get_apps_by_z_order,get_virtual_desktops
        return AppView,VirtualDesktop,get_apps_by_z_order,get_virtual_desktops
    except ImportError as e: raise RuntimeError("Run: pip install pyvda") from e
def current(): return _api()[1].current()
def get_or_create(name):
    _,V,_,allv=_api()
    for d in allv():
        if (d.name or '').casefold()==name.casefold(): return d
    d=V.create();d.rename(name);return d
def pid_for(hwnd):
    p=ctypes.c_ulong();ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd),ctypes.byref(p));return p.value
def move_hwnd(hwnd,d): _api()[0](hwnd=int(hwnd)).move(d)
def move_pid(pid,d,timeout=30,cancel=None):
    _,_,apps,_=_api();end=time.time()+timeout
    while time.time()<end:
        if cancel and cancel.is_set(): return []
        out=[]
        for a in apps(current_desktop=False):
            try:
                if pid_for(a.hwnd)==pid:a.move(d);out.append(a.hwnd)
            except Exception:pass
        if out:return out
        time.sleep(.25)
    return []
class Controller:
    def __init__(self,name):self.name=name;self.desktop=None;self.last=None;self.error='';self._lock=threading.Lock()
    def ensure(self):
        with self._lock:
            if not self.desktop:
                origin=current();self.desktop=get_or_create(self.name)
                if origin.id!=self.desktop.id:self.last=origin
            return self.desktop
    def move(self,pid,cancel=None):
        x=move_pid(pid,self.ensure(),cancel=cancel)
        if not x:self.error=f"No Edge window found for PID {pid}"
        return x
    def show(self):
        try:
            here=current();target=self.ensure()
            if here.id!=target.id:self.last=here
            target.go();return True
        except Exception as e:self.error=str(e);return False
    def back(self):
        if not self.last:return False
        try:self.last.go();return True
        except Exception as e:self.error=str(e);return False
