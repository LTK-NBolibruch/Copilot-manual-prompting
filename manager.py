"""Independent Copilot API sessions on a dedicated Windows 11 virtual desktop."""
import argparse,logging,os,threading,time
from datetime import datetime
import config,procs,server,desktops
from conversations import ConversationStore
from copilot import BrowserHost,CopilotSession,SessionCancelled
from replay import ReplayStore,replay_prefix
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
    def __init__(self,n,port,desk,replay):self.number,self.port,self.desk,self.replay=n,port,desk,replay;self.state='off';self.error='';self.host=self.session=self.httpd=None;self.cancel=threading.Event();self.lock=threading.Lock();self.removed=False
    @property
    def url(self):return f"http://{config.HOST}:{self.port}/v1"
    def start(self,store):
        if self.state not in ('off','error'):return
        self.state='starting';self.error='';self.cancel=threading.Event();threading.Thread(target=self._start,args=(store,),daemon=True).start()
    def _start(self,store):
        with self.lock:
            h=s=q=None
            try:
                os.makedirs(profile(self.port),exist_ok=True);q=server.make_server(self.port,None,store,serve=False,replay=self.replay)
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
def run(store,replay):
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
    history=tk.LabelFrame(root,text='Conversation replay',padx=8,pady=8);history.pack(fill='x',padx=10,pady=(0,8))
    selected_history=tk.StringVar();selected_start=tk.StringVar();history_search=tk.StringVar();history_map={};start_map={};history_status=tk.StringVar(value='Select any local or imported Copilot conversation.');history_limit=[100]
    tk.Label(history,text='Search').grid(row=0,column=0,sticky='w');search=ttk.Entry(history,textvariable=history_search,width=52);search.grid(row=0,column=1,sticky='ew',padx=5)
    convo=ttk.Combobox(history,textvariable=selected_history,state='readonly',width=62);convo.grid(row=1,column=1,sticky='ew',padx=5)
    start=ttk.Combobox(history,textvariable=selected_start,state='readonly',width=62);start.grid(row=2,column=1,sticky='ew',padx=5)
    tk.Label(history,text='Conversation').grid(row=1,column=0,sticky='w');tk.Label(history,text='Start from').grid(row=2,column=0,sticky='w')
    history.columnconfigure(1,weight=1);tk.Label(history,textvariable=history_status,anchor='w').grid(row=4,column=0,columnspan=6,sticky='ew',pady=(5,0))
    def refresh_history(*_):
        current_id=history_map.get(selected_history.get(),{}).get('id')
        query=history_search.get().strip().casefold();items=replay.list(limit=history_limit[0]);history_map.clear();labels=[]
        for h in items:
            users=[m for m in h['messages'] if m['role']=='user'];agents=[m for m in h['messages'] if m['role']=='assistant'];turns=max(len(users),len(agents))
            if query and query not in (h['title']+' '+h['source']+' '+' '.join(m['content'] for m in users)).casefold():continue
            source='Imported from Copilot' if h['source']=='copilot-import' else 'Local'
            updated=datetime.fromtimestamp(h['updated']).strftime('%Y-%m-%d %H:%M')
            label=f"{h['title']}  |  {turns} turn{'s' if turns!=1 else ''}  |  {source}  |  {updated}  |  r{h['revision']}"
            labels.append(label);history_map[label]=h
        convo['values']=labels
        keep=next((l for l,h in history_map.items() if h['id']==current_id),None)
        if keep:selected_history.set(keep)
        elif labels:selected_history.set(labels[0])
        else:selected_history.set('')
        refresh_starts();history_status.set(f"Showing {len(labels)} of the most recent {history_limit[0]} conversation(s).")
    def refresh_starts(*_):
        old_id=start_map.get(selected_start.get());h=history_map.get(selected_history.get());start_map.clear();labels=[];turn=0
        if h:
            for m in h['messages']:
                if m['role']=='user':
                    turn+=1;text=' '.join(m['content'].split());label=f"Turn {turn}  |  {text[:110]}";labels.append(label);start_map[label]=m['id']
        start['values']=labels
        keep=next((l for l,i in start_map.items() if i==old_id),None)
        selected_start.set(keep or (labels[0] if labels else ''))
    convo.bind('<<ComboboxSelected>>',refresh_starts)
    search.bind('<KeyRelease>',lambda _e:refresh_history())
    def preview():
        h=history_map.get(selected_history.get());sid=start_map.get(selected_start.get())
        if not h:return
        snap={'history':h,'start_message_id':sid,'token':'rpl_preview'};text=replay_prefix('rpl_preview',replay.slice(snap),'[next client query]')
        w=tk.Toplevel(root);w.title('Replay preview · '+h['title']);w.geometry('900x650');w.minsize(600,400)
        tk.Label(w,text=h['title'],font=('Segoe UI',11,'bold'),anchor='w').pack(fill='x',padx=10,pady=(10,2))
        tk.Label(w,text='Starting at: '+selected_start.get(),anchor='w').pack(fill='x',padx=10,pady=(0,6))
        frame=tk.Frame(w);frame.pack(fill='both',expand=True,padx=10,pady=4)
        t=tk.Text(frame,wrap='word',undo=False);ys=ttk.Scrollbar(frame,orient='vertical',command=t.yview);t.configure(yscrollcommand=ys.set)
        ys.pack(side='right',fill='y');t.pack(side='left',fill='both',expand=True);t.insert('1.0',text);t.configure(state='disabled')
        buttons=tk.Frame(w);buttons.pack(fill='x',padx=10,pady=(4,10))
        def copy_all():w.clipboard_clear();w.clipboard_append(text);w.update()
        ttk.Button(buttons,text='Copy all',command=copy_all).pack(side='left');ttk.Button(buttons,text='Close',command=w.destroy).pack(side='right')
    def import_copilot():
        x=next((x for x in xs if x.session and x.state=='on'),None)
        if not x:history_status.set('Start a session before importing from Copilot.');return
        history_status.set('Importing visible Copilot history links…')
        def work():
            try:
                results=x.session.import_copilot_history().result(timeout=300);ok=0;errs=[]
                for r in results:
                    if r.get('messages'):replay.upsert(r['messages'],r['url'],source='copilot-import');ok+=1
                    elif r.get('error'):errs.append(r['title']+': '+r['error'])
                root.after(0,lambda:(refresh_history(),history_status.set(f"Imported {ok}; {len(errs)} failed. See logs/server.log.")))
                for e in errs:log.warning('Copilot history import: %s',e)
            except Exception as e:root.after(0,lambda:history_status.set('Import failed: '+str(e)))
        threading.Thread(target=work,daemon=True,name='import-copilot-history').start()
    ttk.Button(history,text='Refresh',command=refresh_history).grid(row=2,column=2,padx=3)
    ttk.Button(history,text='Preview',command=preview).grid(row=1,column=2,padx=3)
    ttk.Button(history,text='Import from Copilot',command=import_copilot).grid(row=1,column=3,rowspan=2,padx=5)
    def load_more():history_limit[0]+=100;refresh_history()
    ttk.Button(history,text='Load 100 more',command=load_more).grid(row=1,column=4,rowspan=2,padx=5)

    table=tk.Frame(root,padx=10);table.pack(fill='both',expand=True)
    for c,t in enumerate(('Session','Port','Endpoint','Session action','State','Replay','')):tk.Label(table,text=t,font=('Segoe UI',9,'bold')).grid(row=0,column=c,sticky='w',padx=5)
    table.columnconfigure(4,weight=1)
    def add():
        count[0]+=1;x=Instance(count[0],next_port(xs),desk,replay);xs.append(x);r=count[0]
        endpoint=ttk.Entry(table,width=32);endpoint.insert(0,x.url);endpoint.configure(state='readonly')
        replay_btn=ttk.Button(table,text='Arm selected',width=16)
        def toggle_replay(x=x,b=replay_btn):
            if replay.peek(x.port):replay.disarm(x.port);b.configure(text='Arm selected');return
            h=history_map.get(selected_history.get());sid=start_map.get(selected_start.get())
            if not h or not sid:history_status.set('Select a conversation and starting turn first.');return
            replay.arm(x.port,h['id'],sid);b.configure(text='Disarm')
        replay_btn.configure(command=toggle_replay)
        ws=[tk.Label(table,text=f"Session {x.number}"),tk.Label(table,text=str(x.port)),endpoint,ttk.Button(table,text='Start',width=12,command=lambda:x.action(store)),tk.Label(table,text='Off'),replay_btn,ttk.Button(table,text='Remove',command=lambda:remove(x))]
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
        for x,w in rows.items():
            w[3].configure(text=x.label(),state='disabled' if x.state in ('stopping','removed') else 'normal');w[4].configure(text=x.status(),fg='#b10e1c' if x.state=='error' else 'black')
            armed=replay.peek(x.port);w[5].configure(text=('Disarm · '+armed['history']['title'][:18]) if armed else 'Arm selected')
        root.after(500,refresh)
    bottom=tk.Frame(root,padx=10,pady=10);bottom.pack(fill='x');ttk.Button(bottom,text='Add',command=add).pack(side='left')
    def close():
        for x in list(xs):x.remove()
        root.after(500,lambda:root.destroy() if not any(x.state in ('starting','stopping') for x in xs) else root.after(500,close))
    root.protocol('WM_DELETE_WINDOW',close);refresh_history();add();refresh();root.minsize(900,0);root.mainloop()
def main():
    ap=argparse.ArgumentParser();ap.parse_args();server.setup_logging();procs.kill_children_on_exit() if config.KILL_ON_EXIT else None;store=ConversationStore() if config.RESUME_CONVERSATIONS else None;replay=ReplayStore(config.CONVERSATION_DB);run(store,replay)
if __name__=='__main__':main()
