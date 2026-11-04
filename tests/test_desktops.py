import threading
import desktops

class D:
    made=[]
    current_obj=None
    def __init__(self,name='',did=None): self.name=name;self.id=did or name;self.gone=0;self.renamed=[]
    def rename(self,n): self.name=n;self.id=n;self.renamed.append(n)
    def go(self): self.gone+=1
    @classmethod
    def create(cls):
        x=cls('','new');cls.made.append(x);return x
    @classmethod
    def current(cls): return cls.current_obj

def api(existing):
    return object,D,lambda **k:[],lambda:existing

def test_get_or_create_reuses_named_desktop(monkeypatch):
    d=D('Copilot Sessions');monkeypatch.setattr(desktops,'_api',lambda:api([d]))
    assert desktops.get_or_create() is d and not D.made

def test_get_or_create_creates_and_renames(monkeypatch):
    D.made.clear();monkeypatch.setattr(desktops,'_api',lambda:api([]))
    d=desktops.get_or_create();assert d.name=='Copilot Sessions' and d.renamed==['Copilot Sessions']

def test_controller_captures_origin_show_and_go_back(monkeypatch):
    origin,target=D('Work','work'),D('Copilot Sessions','copilot')
    D.current_obj=origin
    monkeypatch.setattr(desktops,'_api',lambda:api([target]))
    c=desktops.DesktopController(); assert c.ensure() is target and c.last_desktop is origin
    assert c.show() and target.gone==1
    assert c.go_back() and origin.gone==1

def test_move_browser_failure_is_reported(monkeypatch):
    c=desktops.DesktopController();c.desktop=D('Copilot Sessions')
    monkeypatch.setattr(desktops,'move_pid',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('no window')))
    assert c.move_browser(10)==[] and c.error=='no window'
