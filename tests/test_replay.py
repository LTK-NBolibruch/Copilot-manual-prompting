import tempfile,os
from replay import ReplayStore,replay_prefix,MARK_RE

def test_history_arm_consume_and_return_marker():
 fd,path=tempfile.mkstemp();os.close(fd)
 try:
  r=ReplayStore(path);hid=r.upsert([{'role':'user','content':'one'},{'role':'assistant','content':'two'},{'role':'user','content':'three'}],'https://x/chat/conversation/123')
  h=r.get(hid);snap=r.arm(5000,hid,h['messages'][2]['id']);assert r.peek(5000)
  got=r.consume(5000);assert not r.peek(5000);text=replay_prefix(got['token'],r.slice(got),'four')
  assert 'three' in text and 'one' not in text and MARK_RE.search(text)
  resolved=r.resolve_messages(got['token']);assert resolved['history']['id']==hid and resolved['port']==5000
 finally:os.unlink(path)

def test_replay_marker_is_found_inside_returned_assistant_content():
 fd,path=tempfile.mkstemp();os.close(fd)
 try:
  r=ReplayStore(path);hid=r.upsert([{'role':'user','content':'q'},{'role':'assistant','content':'a'}],'u');h=r.get(hid);x=r.arm(1,hid,h['messages'][0]['id']);r.consume(1)
  assert r.find_marker([{'role':'assistant','content':replay_prefix(x['token'],h['messages'],'next')},{'role':'user','content':'later'}])==x['token']
 finally:os.unlink(path)

def test_clean_store_never_saves_replay_prefix_as_history():
 from replay import clean_messages
 x=clean_messages([{'role':'assistant','content':'<think>x</think>visible'}]);assert x[0]['content']=='visible'

def test_import_wrappers_removed_and_title_is_real_query():
 from replay import clean_messages,title_for
 msgs=clean_messages([{'role':'user','content':'You said:\nWhat is headway?'},{'role':'assistant','content':'Copilot said:\nTime between trains.'}])
 assert msgs[0]['content']=='What is headway?' and msgs[1]['content']=='Time between trains.'
 assert title_for(msgs)=='What is headway?'

def test_canonical_after_replay_discards_monolithic_transcript():
 fd,path=tempfile.mkstemp();os.close(fd)
 try:
  r=ReplayStore(path);hid=r.upsert([{'role':'user','content':'Q1'},{'role':'assistant','content':'A1'}],'u');h=r.get(hid);x=r.arm(9,hid,h['messages'][0]['id']);r.consume(9)
  synthetic=replay_prefix(x['token'],h['messages'],'Q2')+'A2'
  canon=r.canonical_after_replay(x['token'],[{'role':'assistant','content':synthetic},{'role':'user','content':'Q3'}])
  assert [m['content'] for m in canon]==['Q1','A1','Q3']
 finally:os.unlink(path)
