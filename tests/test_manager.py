import os,time,threading
import config,manager
class Desk:
 def move(self,pid,cancel=None):return [pid]
 def show(self):return True
 def back(self):return True
class Srv:
 class H:session=None
 RequestHandlerClass=H
 def shutdown(self):pass
 def server_close(self):pass
class Host:
 def __init__(self,**k):self.pid=10;self.cdp_url='x';self.auth_state='signed_in';self.auth_verified=False
 def set_auth_state(self,state):self.auth_state=state
 def wait_ready(self,*a):pass
 def stop(self,**k):return True
class Session:
 pending=0
 def __init__(self,**k):pass
 def close(self,**k):return True
 def abort(self):pass
def setup_function():
 manager.BrowserHost=Host;manager.CopilotSession=Session;manager.server.make_server=lambda *a,**k:Srv();manager.server.serve_in_background=lambda x:None
def test_action_labels_are_unambiguous(tmp_path):
 config.SESSION_PROFILE_ROOT=str(tmp_path);x=manager.Instance(1,5000,Desk(),None);assert x.label()=='Start';x.start(None);time.sleep(.1);assert x.label()=='Stop';x.action(None);time.sleep(.1);assert x.label()=='Start'
def test_row_has_no_browser_action_column():
 src=open(manager.__file__,encoding='utf8').read();assert "'Browser action'" not in src and 'Show Copilot Sessions desktop' in src
def test_separate_profiles(tmp_path):
 config.SESSION_PROFILE_ROOT=str(tmp_path);assert manager.profile(5000)!=manager.profile(5001)
def test_process_profiles_include_sessions(tmp_path):
 config.SESSION_PROFILE_ROOT=str(tmp_path);os.makedirs(manager.profile(5000));assert manager.profile(5000) in manager.profiles()


def test_show_switches_before_creating_return_dialog():
 src=open(manager.__file__,encoding='utf8').read();body=src[src.index('    def show_copilot_desktop():'):src.index('    table=tk.Frame')];assert body.index('desk.show()')<body.index('return_dialog()');assert 'desktops.move_hwnd(w.winfo_id()' not in src

def test_endpoint_is_directly_populated():
 src=open(manager.__file__,encoding='utf8').read();assert "endpoint.insert(0,x.url)" in src and 'textvariable=url' not in src

def test_ui_prefers_verified_operational_auth():
 x=manager.Instance(1,5000,Desk(),None);x.state='on';x.host=Host();x.host.auth_state='signed_out';x.host.auth_verified=True
 assert x.status().startswith('Ready · signed in')

def test_replay_ui_has_search_paging_and_scrollable_preview():
 src=open(manager.__file__,encoding='utf8').read()
 for text in ('history_search','Load 100 more','ttk.Scrollbar','Copy all'):
  assert text in src
