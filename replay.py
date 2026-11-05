"""Durable conversation library and one-shot replay coordination.

Replay output is one ordinary assistant content stream. The first request to an armed endpoint gets
an annotated transcript prefix, its current user query, then the live Copilot answer. When a client
sends that monolithic assistant message back, the marker reconnects it to the structured history and
original Copilot chat; the synthetic transcript is never pasted into the website prompt.
"""
import json,re,secrets,sqlite3,threading,time,uuid
from conversations import strip_reasoning,text_of
MARK_RE=re.compile(r"^\[COPILOT_REPLAY v1 (rpl_[A-Za-z0-9_-]+)\]",re.M)
WRAPPER_RE = re.compile(r"^(?:you said|copilot said|you|user|copilot|agent|assistant)\s*:\s*", re.I)

def strip_wrapper(text):
 text=(text or '').strip()
 while True:
  cleaned=WRAPPER_RE.sub('',text,count=1).strip()
  if cleaned==text:return cleaned
  text=cleaned

def clean_messages(messages):
 out=[]
 for m in messages or []:
  if not isinstance(m,dict):continue
  role=m.get('role','user'); text=strip_wrapper(text_of(m.get('content')))
  if role=='assistant':text=strip_reasoning(text)
  if text:out.append({'id':m.get('id') or 'msg_'+uuid.uuid4().hex[:16],'role':role,'content':text})
 return out

def title_for(msgs):
 return next((strip_wrapper(m['content']).splitlines()[0][:100] for m in msgs if m['role']=='user' and strip_wrapper(m['content'])),'Untitled conversation')

def replay_prefix(token,history,current):
 out=[f"[COPILOT_REPLAY v1 {token}]","","## Previous Conversation",""]
 for m in history:
  role='User' if m['role']=='user' else 'Agent' if m['role']=='assistant' else m['role'].title()
  out += [f"### {role}",m['content'],""]
 out += ["## Current Query","","### User",current,"","## Current Response","","### Agent",""]
 return '\n'.join(out)

class ReplayStore:
 def __init__(self,path):
  self.lock=threading.RLock();self.db=sqlite3.connect(path,check_same_thread=False)
  with self.lock:
   self.db.executescript('''
   CREATE TABLE IF NOT EXISTS history_library(id TEXT PRIMARY KEY,title TEXT,chat_url TEXT UNIQUE,
    source TEXT,model TEXT,messages_json TEXT,revision INTEGER,complete INTEGER,created REAL,updated REAL);
   CREATE TABLE IF NOT EXISTS replay_tokens(token TEXT PRIMARY KEY,history_id TEXT,start_message_id TEXT,
    endpoint_port INTEGER,created REAL,consumed REAL);
   ''');self.db.commit()
  self.armed={}
  self._normalize_existing()
 def _normalize_existing(self):
  """Repair v27 wrapper-only titles/messages in place on first v28 startup."""
  with self.lock:
   rows=self.db.execute('SELECT id,messages_json FROM history_library').fetchall()
   for hid,raw in rows:
    try:msgs=clean_messages(json.loads(raw or '[]'))
    except Exception:continue
    self.db.execute('UPDATE history_library SET title=?,messages_json=? WHERE id=?',
                    (title_for(msgs),json.dumps(msgs),hid))
   self.db.commit()
 def upsert(self,messages,chat_url,model=None,complete=True,source='local',history_id=None):
  msgs=clean_messages(messages); now=time.time()
  with self.lock:
   row=self.db.execute('SELECT id,revision,created FROM history_library WHERE chat_url=?',(chat_url,)).fetchone() if chat_url else None
   hid=(row[0] if row else history_id) or 'conv_'+uuid.uuid4().hex
   rev=(row[1]+1 if row else 1);created=row[2] if row else now
   self.db.execute('''INSERT INTO history_library VALUES(?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(id) DO UPDATE SET title=excluded.title,chat_url=excluded.chat_url,source=excluded.source,
    model=excluded.model,messages_json=excluded.messages_json,revision=excluded.revision,
    complete=excluded.complete,updated=excluded.updated''',(hid,title_for(msgs),chat_url,source,model,json.dumps(msgs),rev,int(complete),created,now));self.db.commit()
  return hid
 def list(self,limit=None,offset=0):
  sql='SELECT id,title,chat_url,source,model,messages_json,revision,complete,updated FROM history_library ORDER BY updated DESC'
  args=()
  if limit is not None: sql += ' LIMIT ? OFFSET ?'; args=(limit,offset)
  with self.lock:rows=self.db.execute(sql,args).fetchall()
  return [dict(id=r[0],title=r[1],chat_url=r[2],source=r[3],model=r[4],messages=json.loads(r[5]),revision=r[6],complete=bool(r[7]),updated=r[8]) for r in rows]
 def get(self,hid):
  return next((x for x in self.list() if x['id']==hid),None)
 def delete(self,hid):
  with self.lock:self.db.execute('DELETE FROM history_library WHERE id=?',(hid,));self.db.commit()
 def arm(self,port,hid,start_message_id):
  h=self.get(hid)
  if not h:raise KeyError(hid)
  token='rpl_'+secrets.token_urlsafe(18);snap=dict(token=token,history=h,start_message_id=start_message_id)
  with self.lock:
   self.armed[port]=snap
   self.db.execute('INSERT INTO replay_tokens VALUES(?,?,?,?,?,NULL)',(token,hid,start_message_id,port,time.time()));self.db.commit()
  return snap
 def disarm(self,port):
  with self.lock:return self.armed.pop(port,None)
 def peek(self,port):
  with self.lock:return self.armed.get(port)
 def consume(self,port):
  with self.lock:
   x=self.armed.pop(port,None)
   if x:self.db.execute('UPDATE replay_tokens SET consumed=? WHERE token=?',(time.time(),x['token']));self.db.commit()
  return x
 def resolve_messages(self,token):
  with self.lock:r=self.db.execute('SELECT history_id,start_message_id,endpoint_port FROM replay_tokens WHERE token=?',(token,)).fetchone()
  if not r:return None
  h=self.get(r[0]);return dict(history=h,start_message_id=r[1],port=r[2]) if h else None
 def canonical_after_replay(self,token,messages):
  resolved=self.resolve_messages(token)
  if not resolved:return None
  base=resolved['history']['messages']
  marker_index=-1
  for i,m in enumerate(messages or []):
   if m.get('role')=='assistant' and MARK_RE.search(text_of(m.get('content'))):marker_index=i
  tail=clean_messages((messages or [])[marker_index+1:])
  return clean_messages(base+tail)
 def find_marker(self,messages):
  for m in reversed(messages or []):
   if m.get('role')=='assistant':
    x=MARK_RE.search(text_of(m.get('content')))
    if x:return x.group(1)
  return None
 def slice(self,snap):
  msgs=snap['history']['messages'];sid=snap['start_message_id'];idx=next((i for i,m in enumerate(msgs) if m['id']==sid),0)
  return msgs[idx:]
 def close(self):self.db.close()
