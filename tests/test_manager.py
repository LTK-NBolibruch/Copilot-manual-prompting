import os, socket, threading, time
import pytest
import config, manager
from copilot import SessionCancelled

class FakeServer:
    class H: session=None
    RequestHandlerClass=H
    def server_close(self): pass
    def shutdown(self): pass

class FakeHost:
    made=[]
    def __init__(self, show_window=False, profile_dir=None, **kw):
        self.visible=show_window; self.profile_dir=profile_dir; self.cdp_url='http://x'; self.pid=100+len(self.made)
        self.can_show=True; self.needs_sign_in=False; self.made.append(self)
    def wait_ready(self,cancel=None):
        if cancel and cancel.is_set(): raise SessionCancelled()
    def stop(self,*a,**k): pass
    def set_visible(self,v): self.visible=v

class FakeSession:
    made=[]
    pending=0; needs_sign_in=False; alive=True
    def __init__(self, **kw): self.kw=kw; self.made.append(self)
    def close(self,timeout=0): self.alive=False; return True
    def abort(self): self.alive=False

@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'PROFILE_DIR',str(tmp_path/'base'))
    monkeypatch.setattr(config,'SESSION_PROFILE_ROOT',str(tmp_path/'sessions'))
    monkeypatch.setattr(manager,'BrowserHost',FakeHost)
    monkeypatch.setattr(manager,'CopilotSession',FakeSession)
    monkeypatch.setattr(manager.server,'make_server',lambda *a,**k:FakeServer())
    monkeypatch.setattr(manager.server,'serve_in_background',lambda s:None)
    FakeHost.made.clear(); FakeSession.made.clear()

def test_seed_profile_copies_once_and_excludes_runtime_files(tmp_path):
    base=tmp_path/'base'; base.mkdir(); (base/'Cookies').write_text('signed in')
    (base/'SingletonLock').write_text('lock'); (base/'Cache').mkdir(); (base/'Cache'/'x').write_text('x')
    dst=manager.seed_profile(5000)
    assert open(os.path.join(dst,'Cookies')).read()=='signed in'
    assert not os.path.exists(os.path.join(dst,'SingletonLock')) and not os.path.exists(os.path.join(dst,'Cache'))
    (base/'Cookies').write_text('changed')
    assert manager.seed_profile(5000)==dst and open(os.path.join(dst,'Cookies')).read()=='signed in'

def test_each_session_has_separate_browser_and_profile():
    a,b=manager.Instance(1,5000),manager.Instance(2,5001)
    a.start(None,wait=True); b.start(None,wait=True)
    assert a.state==b.state=='on'
    assert a.host is not b.host and a.host.profile_dir!=b.host.profile_dir
    assert a.host.profile_dir.endswith('session-5000') and b.host.profile_dir.endswith('session-5001')
    assert len(FakeHost.made)==2 and len(FakeSession.made)==2
    a.stop(wait=True); b.stop(wait=True)

def test_cancel_start(monkeypatch):
    gate=threading.Event()
    def slow(port): gate.wait(); return manager.session_profile(port)
    monkeypatch.setattr(manager,'seed_profile',slow)
    i=manager.Instance(1,5000); i.start(None); time.sleep(.05); i.cancel_start(); gate.set()
    end=time.time()+2
    while i.state=='starting' and time.time()<end: time.sleep(.02)
    assert i.state=='off' and not FakeHost.made

def test_remove_stops_own_browser_only():
    a,b=manager.Instance(1,5000),manager.Instance(2,5001)
    a.start(None,wait=True); b.start(None,wait=True)
    ah,bh=a.host,b.host
    stopped=[]; ah.stop=lambda *a,**k:stopped.append('a'); bh.stop=lambda *a,**k:stopped.append('b')
    a.remove(); end=time.time()+2
    while a.state=='stopping' and time.time()<end: time.sleep(.02)
    assert stopped==['a'] and b.state=='on'
    b.stop(wait=True)

def test_show_is_per_session_and_restarts_only_that_browser():
    a,b=manager.Instance(1,5000),manager.Instance(2,5001)
    a.start(None,wait=True); b.start(None,wait=True)
    original_b=b.host
    a.set_visible(None)
    end=time.time()+3
    while a.state != "on" and time.time()<end: time.sleep(.02)
    assert a.state == "on" and a.host.visible
    assert b.host is original_b and not b.host.visible
    a.stop(wait=True); b.stop(wait=True)


def test_sign_in_status_follows_live_browser_host():
    i=manager.Instance(1,5000); i.start(None,wait=True)
    i.session.needs_sign_in=True       # stale startup value
    i.host.needs_sign_in=True
    assert i.status_text().endswith("not signed in")
    i.host.needs_sign_in=False
    assert "not signed in" not in i.status_text()
    i.stop(wait=True)

def test_next_port():
    assert manager.next_port([])==config.PORT
    assert manager.next_port([manager.Instance(1,config.PORT)])==config.PORT+1


def test_sign_in_status_follows_live_browser_host():
    i=manager.Instance(1,5000); i.start(None,wait=True)
    i.session.needs_sign_in=True       # stale value from the startup login redirect
    i.host.needs_sign_in=True
    assert i.status_text().endswith("not signed in")
    i.host.needs_sign_in=False         # browser monitor sees Copilot after manual sign-in
    assert "not signed in" not in i.status_text()
    i.stop(wait=True)
