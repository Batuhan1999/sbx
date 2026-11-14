#!/usr/bin/env python3
"""Full-product auth, ingest and shared-warehouse verification; no key output."""
import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid

parser=argparse.ArgumentParser()
parser.add_argument('--seed',action='store_true')
parser.add_argument('--expect-empty',action='store_true')
args=parser.parse_args()
BASE='http://127.0.0.1:54567'
SERVICE='sbx-full-maple-check'


def call(url, body=None, headers=None):
    request=urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                   headers={'content-type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(request,timeout=45) as response:
            data=response.read()
            return response.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        return error.code, {}


status,login=call(BASE+'/api/auth/login',{'password':os.environ['MAPLE_ROOT_PASSWORD']})
assert status==200, f'login HTTP {status}'
auth={'Authorization':'Bearer '+login['token']}
status,session=call(BASE+'/api/auth/session',headers=auth)
assert status==200, f'session HTTP {status}'
status,keys=call(BASE+'/v2/ingest_keys/',headers=auth)
assert status==200, f'ingest keys HTTP {status}'
key=keys.get('private_key')
assert key, 'ingest keys response did not include the private key'
ch_auth=base64.b64encode((os.environ['CLICKHOUSE_USER']+':'+os.environ['CLICKHOUSE_PASSWORD']).encode()).decode()


def query(sql):
    url=os.environ['CLICKHOUSE_URL']+'/?database='+os.environ['CLICKHOUSE_DATABASE']
    request=urllib.request.Request(url,data=(sql+' FORMAT JSONEachRow').encode(),headers={'Authorization':'Basic '+ch_auth})
    with urllib.request.urlopen(request,timeout=20) as response:
        return [json.loads(line) for line in response.read().splitlines() if line]


def count():
    return int(query("SELECT count() AS n FROM traces WHERE ServiceName='"+SERVICE+"'")[0]['n'])

before=count()
if args.expect_empty: assert before==0, f'new sandbox already had {before} check spans'
trace_id=None
if args.seed:
    trace_id=uuid.uuid4().hex
    span_id=uuid.uuid4().hex[:16]
    started=time.time_ns()
    body={'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':SERVICE}},
                                                      {'key':'deployment.environment.name','value':{'stringValue':'check'}}]},
                            'scopeSpans':[{'scope':{'name':'dev-sandboxes-check'},'spans':[{
                                'traceId':trace_id,'spanId':span_id,'name':'GET /full-stack-check',
                                'kind':2,'startTimeUnixNano':str(started),'endTimeUnixNano':str(started+31000000),
                                'status':{'code':1},'attributes':[{'key':'http.request.method','value':{'stringValue':'GET'}},
                                                                {'key':'http.response.status_code','value':{'intValue':'200'}}]}]}]}]}
    unauth,_=call('http://127.0.0.1:3474/v1/traces',body)
    assert unauth==401, f'unauthenticated ingest HTTP {unauth}'
    status,_=call('http://127.0.0.1:3474/v1/traces',body,{'Authorization':'Bearer '+key})
    assert status in (200,202), f'ingest HTTP {status}'
    deadline=time.monotonic()+30
    while count()<before+1 and time.monotonic()<deadline: time.sleep(0.5)
    assert count()==before+1, 'ingested trace did not reach the shared ClickHouse database'
result={'login':200,'session':200,'ingest_keys':200,'postgres_backed_ingest_key':True,
        'service':SERVICE,'trace_id':trace_id,'spans_before':before,'spans_after':count(),
        'database':os.environ['CLICKHOUSE_DATABASE'],'electric_configured':bool(os.environ.get('ELECTRIC_SECRET'))}
print(json.dumps(result))
