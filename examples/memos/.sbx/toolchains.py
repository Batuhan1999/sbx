#!/usr/bin/env python3
"""Install upstream toolchains in this user's directory, checking release hashes."""
import argparse,hashlib,json,platform,subprocess,tarfile,tempfile,urllib.request
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--node-major',type=int,default=24);p.add_argument('--go');a=p.parse_args()
if platform.machine()!='x86_64':raise SystemExit('this example currently targets Linux x86_64')
root=Path.home()/'.local';root.mkdir(exist_ok=True)

def get(url):
 with urllib.request.urlopen(url,timeout=120) as r:return r.read()

def version(path):
 try:return subprocess.check_output([str(path),'--version'],text=True).strip()
 except (OSError,subprocess.CalledProcessError):return ''

def install(url,digest,target,prefix):
 with tempfile.TemporaryDirectory(dir=root,prefix='.sbx-toolchain-') as tmp:
  archive=Path(tmp)/'archive.tar.gz';archive.write_bytes(get(url))
  if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest:raise SystemExit('upstream toolchain checksum mismatch')
  with tarfile.open(archive) as t:t.extractall(tmp,filter='data')
  staged=Path(tmp)/prefix
  if target.exists():raise SystemExit('refusing to overwrite an existing toolchain at '+str(target))
  staged.rename(target)
node=root/'node'
if not version(node/'bin/node').startswith('v'+str(a.node_major)+'.'):
 if not node.exists():
  releases=json.loads(get('https://nodejs.org/dist/index.json'))
  release=next(r for r in releases if r['version'].startswith('v'+str(a.node_major)+'.') and 'linux-x64' in r['files'])
  v=release['version'];name=f'node-{v}-linux-x64.tar.gz';base=f'https://nodejs.org/dist/{v}/'
  sums=dict((line.split()[1],line.split()[0]) for line in get(base+'SHASUMS256.txt').decode().splitlines())
  install(base+name,sums[name],node,f'node-{v}-linux-x64')
 else:raise SystemExit('the existing local Node toolchain has a different major version')
if a.go:
 go=root/'go'
 current=subprocess.run([str(go/'bin/go'),'version'],capture_output=True,text=True).stdout if (go/'bin/go').exists() else ''
 if ('go'+a.go+' ') not in current:
  releases=json.loads(get('https://go.dev/dl/?mode=json&include=all'))
  release=next(r for r in releases if r['version']=='go'+a.go)
  f=next(f for f in release['files'] if f['os']=='linux' and f['arch']=='amd64' and f['kind']=='archive')
  install('https://go.dev/dl/'+f['filename'],f['sha256'],go,'go')
print('upstream toolchains ready')
