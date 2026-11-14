#!/usr/bin/env python3
"""Exercise real note and attachment APIs without printing credentials."""
import argparse,base64,http.cookiejar,json,os,urllib.error,urllib.request
p=argparse.ArgumentParser();p.add_argument('--seed',action='store_true');p.add_argument('--expect-empty',action='store_true');a=p.parse_args()
base='http://127.0.0.1:5230';marker='SBX Memos source check'
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def call(path,body=None,method=None,auth=True):
 headers={'Content-Type':'application/json'}
 if auth and token:headers['Authorization']='Bearer '+token
 request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers,method=method)
 try:
  with client.open(request,timeout=30) as r:
   data=r.read();return r.status,json.loads(data) if data else {}
 except urllib.error.HTTPError as e:return e.code,{}
token=None
status,login=call('/api/auth/signin',{'passwordCredentials':{'username':'sbx-admin','password':os.environ['MEMOS_ADMIN_PASSWORD']}},auth=False)
assert status==200,f'login HTTP {status}'
token=login['accessToken']
status,user=call('/api/auth/me');assert status==200,'authenticated user lookup failed'
status,listed=call('/api/memos');assert status==200,'memo list failed'
notes=[m for m in listed.get('memos',[]) if marker in m.get('content','')]
if a.expect_empty:assert not notes,'new workspace already has check notes'
if a.seed and not notes:
 status,note=call('/api/memos',{'content':marker+'\n\nCreated from the full source application. #sandbox-check','visibility':'PRIVATE'})
 assert status==200,f'create memo HTTP {status}'
 status,note=call('/api/'+note['name']+'?updateMask=content',{'name':note['name'],'content':note['content']+'\n\nEdited through the actual Memos API.'},method='PATCH')
 assert status==200,f'edit memo HTTP {status}'
 content=base64.b64encode(b'sbx attachment round trip\n').decode()
 status,attachment=call('/api/attachments',{'filename':'sbx-check.txt','type':'text/plain','content':content,'memo':note['name']})
 assert status==200,f'attachment upload HTTP {status}'
 notes=[note]
assert len(notes)==1,'expected one stored check note'
note=notes[0]
status,stored=call('/api/'+note['name']);assert status==200 and 'Edited through' in stored['content'],'stored note read-back failed'
# A fresh client has neither bearer token nor refresh cookie.
request=urllib.request.Request(base+'/api/'+note['name'])
try:
 urllib.request.urlopen(request,timeout=15);raise AssertionError('private note was readable without auth')
except urllib.error.HTTPError as e:assert e.code in (401,403,404),f'private note HTTP {e.code}'
status,attachments=call('/api/attachments');assert status==200
files=[f for f in attachments.get('attachments',[]) if f.get('filename')=='sbx-check.txt' and f.get('memo')==note['name']]
assert len(files)==1,'linked attachment was not retained'
file_path='/file/'+files[0]['name']+'/'+files[0]['filename']
request=urllib.request.Request(base+file_path,headers={'Authorization':'Bearer '+token})
with client.open(request,timeout=20) as response:
 assert response.read()==b'sbx attachment round trip\n','attachment bytes did not round-trip'
try:
 urllib.request.urlopen(base+file_path,timeout=15);raise AssertionError('private attachment was readable without auth')
except urllib.error.HTTPError as e:assert e.code in (401,403,404)
print(json.dumps({'login':200,'private_note_create_edit_read':True,'unauthenticated_note_denied':True,'linked_attachment_bytes':True,'unauthenticated_attachment_denied':True,'notes':len(notes),'memo':note['name']}))
