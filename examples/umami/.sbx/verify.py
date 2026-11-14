#!/usr/bin/env python3
"""Use Umami's real login, website, collection and reporting endpoints."""
import argparse,json,os,time,urllib.error,urllib.parse,urllib.request
p=argparse.ArgumentParser();p.add_argument('--seed',action='store_true');p.add_argument('--expect-empty',action='store_true');a=p.parse_args()
base='http://127.0.0.1:3000';marker='SBX Umami source check';token=None
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
def call(path,body=None,auth=True):
 headers={'content-type':'application/json','user-agent':UA}
 if auth and token:headers['authorization']='Bearer '+token
 request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
 try:
  with urllib.request.urlopen(request,timeout=60) as r:
   data=r.read();return r.status,json.loads(data) if data else {}
 except urllib.error.HTTPError as e:return e.code,{}
status,login=call('/api/auth/login',{'username':'admin','password':os.environ['UMAMI_ADMIN_PASSWORD']},auth=False)
assert status==200,f'login HTTP {status}'
token=login['token']
status,websites=call('/api/websites');assert status==200,'websites list failed'
items=websites.get('data',[]) if isinstance(websites,dict) else websites
sites=[w for w in items if w['name']==marker]
if a.expect_empty:assert not sites,'new workspace already had the check site'
if a.seed and not sites:
 status,site=call('/api/websites',{'name':marker,'domain':'sandbox-check.example'})
 assert status==200,f'website creation HTTP {status}'
 sites=[site]
assert len(sites)==1,'expected one stored check website'
site=sites[0];ident=site['id']
params=urllib.parse.urlencode({'startAt':int((time.time()-86400)*1000),'endAt':int((time.time()+3600)*1000)})
path='/api/websites/'+ident+'/stats?'+params
status,stats=call(path);assert status==200,f'stats HTTP {status}'
before=stats.get('pageviews',0)
if a.seed:
 status,event=call('/api/send',{'type':'event','payload':{'website':ident,'hostname':'sandbox-check.example','url':'/sandbox-check','title':'Sandbox source test','screen':'1280x720','language':'en-US'}},auth=False)
 assert status==200,f'collection HTTP {status}'
 deadline=time.monotonic()+30
 while time.monotonic()<deadline:
  status,stats=call(path)
  if status==200 and stats.get('pageviews',0)>before:break
  time.sleep(.5)
 assert stats.get('pageviews',0)>before,'collected pageview missing from analytics report'
else:assert before>=1,'stored analytics pageview was lost'
status,_=call('/api/websites',auth=False);assert status in (401,403),'report accepted unauthenticated access'
print(json.dumps({'login':200,'website_create_read':True,'collection_report_readback':True,'unauthenticated_report_denied':True,'pageviews':stats['pageviews'],'website_id':ident}))
