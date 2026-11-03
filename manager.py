"""Independent Copilot API sessions on a dedicated Windows 11 virtual desktop."""
import argparse,logging,os,threading,time
import config,procs,server,desktops
from conversations import ConversationStore
from copilot import BrowserHost,CopilotSession,SessionCancelled
log=logging.getLogger("manager")
def next_port(xs):return max((x.port for x in xs),default=config.PORT-1)+1
def profile(port):return os.path.abspath(os.path.join(config.SESSION_PROFILE_ROOT,f"session-{port}"))
def profiles(xs=()):
    d={os.path.abspath(config.PROFILE_DIR):"Base profile"};root=os.path.abspath(config.SESSION_PROFILE_ROOT)
    if os.path.isdir(root):
        for n in os.listdir(root):
            p=os.path.join(root,n)
            if os.path.isdir(p):d[p]=n
    for x in xs:d[profile(x.port)]=f"Session {x.number} · {x.port}"
    return d
class Instance:
    def __init__(self,n,port,desk):self.number,self.port,self.desk=n,port,desk;self.state='off';self.error='';self.host=self.session=self.httpd=None;self.cancel=threading.Event();self.lock=threading.Lock();self.removed=False
    @property
    def url(self):return f"http://{config.HOST}:{self.port}/v1"
    def start(self,store):
        if self.state not in ('off','error'):return
        self.state='starting';self.error='';self.cancel=threading.Event();threading.Thread(target=self._start,args=(store,),daemon=True).start()
    def _start(self,store):
        with self.lock:
            h=s=q=None
            try:
                os.makedirs(profile(self.port),exist_ok=True);q=server.make_server(self.port,None,store,serve=False)
                h=BrowserHost(show_window=True,profile_dir=profile(self.port));self.host=h;h.wait_ready(self.cancel)
                if not self.desk.move(h.pid,self.cancel):raise RuntimeError(self.desk.error)
                s=CopilotSession(show_window=True,cdp_url=h.cdp_url,name=str(self.port),cancel=self.cancel,is_visible=lambda:True,reuse_existing=True,auth_callback=h.set_auth_state)
                q.RequestHandlerClass.session=s;server.serve_in_background(q);self.httpd,self.session,self.state=q,s,'on'
            except SessionCancelled:self.state='removed' if self.removed else 'off'
            except Exception as e:self.error=str(e);self.state='error';log.exception("Session start failed")
            finally:
                if self.state!='on':
                    if s:s.abort()
                    if q:q.server_close()
                    if h:h.stop(final=True)
                    self.host=self.session=self.httpd=None
    def action(self,store):
        if self.state=='starting':self.cancel.set()
        elif self.state=='on':self.stop()
        elif self.state in ('off','error'):self.start(store)
    def stop(self,force=False):
        if self.state=='starting':self.cancel.set();return
        if self.state not in ('on','error'):return
        self.state='stopping';threading.Thread(target=self._stop,args=(force,),daemon=True).start()
    def _stop(self,force):
        with self.lock:
            try:
                if self.httpd:self.httpd.shutdown();self.httpd.server_close()
                if self.session and (force or not self.session.close(timeout=config.STOP_TIMEOUT)):self.session.abort()
                if self.host:self.host.stop(final=True)
            finally:self.httpd=self.session=self.host=None;self.state='removed' if self.removed else 'off'
    def remove(self):self.removed=True;self.cancel.set();self.stop(True)
    def label(self):return {'off':'Start','starting':'Cancel start','on':'Stop','stopping':'Working…','error':'Retry','removed':'Removed'}[self.state]
    def status(self):
        if self.state=='on':
            a='signed_in' if getattr(self.host,'auth_verified',False) else getattr(self.host,'auth_state','checking');return {'signed_in':'Ready · signed in','signed_out':'Sign-in required','checking':'Ready · checking sign-in'}[a]+f" · PID {self.host.pid}"
        if self.state=='error':return 'Error · '+self.error
        return {'off':'Off','starting':'Starting Edge…','stopping':'Stopping Edge cleanly…','removed':'Removed'}[self.state]
def run(store):
    import tkinter as tk
    from tkinter import ttk
    root=tk.Tk();root.title('Copilot sessions');desk=desktops.Controller(config.VIRTUAL_DESKTOP_NAME);xs=[];rows={};count=[0];ret=[None]
    def return_dialog():
        if ret[0] is not None:
            try:
                ret[0].deiconify();ret[0].lift();return
            except tk.TclError:ret[0]=None
        # Created after desk.show(), so Windows places it directly on Copilot Sessions.
        # Moving a brand-new Tk HWND through pyvda can fail with COM "Element not found".
        w=tk.Toplevel(root);w.title('Copilot Sessions')
        tk.Label(w,text='Copilot Sessions',font=('Segoe UI',12,'bold')).pack(padx=18,pady=(18,6))
        tk.Label(w,text='Managed Edge windows live on this desktop.').pack(padx=18,pady=6)
        ttk.Button(w,text='Go back to last desktop',command=desk.back).pack(padx=18,pady=(10,18))
        w.protocol('WM_DELETE_WINDOW',w.withdraw);ret[0]=w
    def show_copilot_desktop():
        if desk.show():return_dialog()
        else:log.error('Could not show %s desktop: %s',desk.name,desk.error)
    top=tk.Frame(root,padx=10,pady=8);top.pack(fill='x');ttk.Button(top,text='Show Copilot Sessions desktop',command=show_copilot_desktop).pack(side='left');ttk.Button(top,text='Processes…',command=lambda:process_dialog()).pack(side='left',padx=6)
    table=tk.Frame(root,padx=10);table.pack(fill='both',expand=True)
    for c,t in enumerate(('Session','Port','Endpoint','Session action','State','')):tk.Label(table,text=t,font=('Segoe UI',9,'bold')).grid(row=0,column=c,sticky='w',padx=5)
    table.columnconfigure(4,weight=1)
    def add():
        count[0]+=1;x=Instance(count[0],next_port(xs),desk);xs.append(x);r=count[0]
        endpoint=ttk.Entry(table,width=32);endpoint.insert(0,x.url);endpoint.configure(state='readonly')
        ws=[tk.Label(table,text=f"Session {x.number}"),tk.Label(table,text=str(x.port)),endpoint,ttk.Button(table,text='Start',width=12,command=lambda:x.action(store)),tk.Label(table,text='Off'),ttk.Button(table,text='Remove',command=lambda:remove(x))]
        for c,w in enumerate(ws):w.grid(row=r,column=c,sticky='w',padx=5,pady=3)
        rows[x]=ws;x.start(store)
    def remove(x):x.remove();[w.destroy() for w in rows.pop(x)];xs.remove(x)
    def process_dialog():
        w=tk.Toplevel(root);w.title('Managed Edge processes');cols=('profile','pid','what','count','owner');tree=ttk.Treeview(w,columns=cols,show='headings',height=10,selectmode='extended');[tree.heading(c,text=c.title()) for c in cols];tree.pack(fill='both',expand=True,padx=10,pady=10);data={}
        active={profile(x.port):x.host.pid for x in xs if x.host and x.host.pid}
        for p,n in profiles(xs).items():
            for g in procs.scan(p,active.get(p)).groups:
                iid=f"{len(data)}";data[iid]=(p,g,g.kind=='ours');tree.insert('', 'end',iid=iid,values=(n,g.pid,'Active (protected)' if g.kind=='ours' else g.what,len(g.pids),g.owner))
        def endsel():
            for i in tree.selection():
                p,g,a=data[i]
                if not a:procs.end_groups(p,pids=[g.pid])
            w.destroy()
        ttk.Button(w,text='End selected',command=endsel).pack(side='left',padx=10,pady=8);ttk.Button(w,text='Close',command=w.destroy).pack(side='right',padx=10,pady=8)
    def refresh():
        for x,w in rows.items():w[3].configure(text=x.label(),state='disabled' if x.state in ('stopping','removed') else 'normal');w[4].configure(text=x.status(),fg='#b10e1c' if x.state=='error' else 'black')
        root.after(500,refresh)
    bottom=tk.Frame(root,padx=10,pady=10);bottom.pack(fill='x');ttk.Button(bottom,text='Add',command=add).pack(side='left')
    def close():
        for x in list(xs):x.remove()
        root.after(500,lambda:root.destroy() if not any(x.state in ('starting','stopping') for x in xs) else root.after(500,close))
    root.protocol('WM_DELETE_WINDOW',close);add();refresh();root.minsize(690,0);root.mainloop()
def main():
    ap=argparse.ArgumentParser();ap.parse_args();server.setup_logging();procs.kill_children_on_exit() if config.KILL_ON_EXIT else None;run(ConversationStore() if config.RESUME_CONVERSATIONS else None)
if __name__=='__main__':main()
