"""CLI is an API client; datasets/benchmarks import the shared engine."""
import argparse
import hashlib
import http.cookiejar
import ssl
import tempfile
import ipaddress
from urllib.parse import urlsplit
import json
import os
import sys
import time
from pathlib import Path
import httpx
from . import config

class Client:
    def __init__(self,server):
        server=server.rstrip('/')
        parsed=urlsplit(server)
        if (parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path or any(c.isspace() for c in server)):
            raise ValueError('Use a Veil HTTP(S) origin without credentials, path, query or fragment')
        if parsed.scheme=='http':
            try:local=parsed.hostname=='localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:local=False
            if not local:raise ValueError('HTTPS is required for network workspaces')
        origin=hashlib.sha256(server.encode()).hexdigest()
        root=Path(os.getenv('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'veil'/'workspaces'
        self.cookie_file=Path(os.getenv('VEIL_WORKSPACE_COOKIE_FILE',str(root/(origin+'.cookies'))))
        jar=http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            if self.cookie_file.stat().st_mode & 0o077:raise ValueError('Workspace cookie file must be private (permissions 600)')
            try:jar.load(ignore_discard=True)
            except (OSError,http.cookiejar.LoadError):raise ValueError('Workspace cookie file is unreadable') from None
        ca=os.getenv('VEIL_CA_BUNDLE')
        self.http=httpx.Client(base_url=server,timeout=60,trust_env=False,cookies=jar,
                               verify=ssl.create_default_context(cafile=ca) if ca else True,
                               headers={'X-Veil-Request':'1'})
        self.initialized=False
    def save_cookies(self):
        self.cookie_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,name=tempfile.mkstemp(dir=self.cookie_file.parent,prefix='.veil-cookies-')
        os.close(fd)
        try:
            jar=http.cookiejar.MozillaCookieJar(name)
            for cookie in self.http.cookies.jar:jar.set_cookie(cookie)
            jar.save(ignore_discard=True)
            os.chmod(name,0o600);os.replace(name,self.cookie_file)
        finally:
            if os.path.exists(name):os.unlink(name)
    def request(self,method,path,**kw):
        if not self.initialized:
            bootstrap=self.http.get('/api/workspace');bootstrap.raise_for_status()
            self.save_cookies();self.initialized=True
        r=self.http.request(method,path,**kw);r.raise_for_status()
        if 'set-cookie' in r.headers:self.save_cookies()
        return r
    def submit(self,path):
        with path.open('rb') as f: return self.request('POST','/api/jobs',files={'file':('upload'+path.suffix,f)}).json()['id']
    def wait(self,jid,quiet=False,timeout=None):
        if timeout is None: timeout=config.TIMEOUT+config.PROVIDER_WAIT_TIMEOUT+30
        start=time.monotonic(); prior=None
        while time.monotonic()-start<timeout:
            j=self.request('GET','/api/jobs/'+jid).json()
            if not quiet and j['status']!=prior: print(jid,j['status'],file=sys.stderr); prior=j['status']
            if j['status'] in config.TERMINAL: return j
            time.sleep(0.4)
        raise TimeoutError('Job is still active; inspect it with veil status '+jid)
    def download(self,jid,output):
        report=self.request('GET',f'/api/jobs/{jid}/report').json()
        output=Path(output)
        names=report.get('artifacts',['report.json'])
        if any((output/n).exists() for n in names): raise FileExistsError('Refusing to overwrite an existing output')
        output.mkdir(parents=True,exist_ok=True)
        saved=[]
        for name in names:
            if name not in {'sanitised.docx','sanitised.pptx','sanitised.xlsx','sanitised.pdf','sanitised.txt','report.json'}: raise ValueError('Unexpected artifact name')
            data=self.request('GET',f'/api/jobs/{jid}/download/{name}').content
            with (output/name).open('xb') as f: f.write(data)
            saved.append(str(output/name))
        return saved

def parser():
    p=argparse.ArgumentParser(prog='veil',description='Veil on-prem document PII redaction POC. Exit: 0 completed; 2 review/blocked; 1 failure; 64 usage.')
    p.add_argument('--server',default=os.getenv('VEIL_SERVER','https://127.0.0.1:8787'))
    sub=p.add_subparsers(dest='command',required=True)
    def common(q):
        q.add_argument('--server',default=argparse.SUPPRESS,help='Veil API endpoint')
        q.add_argument('--json',action='store_true',help='Machine-readable stdout; progress stays on stderr')
    q=sub.add_parser('redact',help='Submit file/directory; wait and download by default'); q.add_argument('input',type=Path); q.add_argument('--output',type=Path,default=Path('sanitised'));q.add_argument('--recursive',action='store_true');q.add_argument('--async',dest='async_',action='store_true');common(q)
    for name in ('status','report','download','delete'):
        q=sub.add_parser(name);q.add_argument('job_id');common(q)
        if name=='download': q.add_argument('--output',type=Path,default=Path('sanitised'))
    q=sub.add_parser('datasets');ds=q.add_subparsers(dest='action',required=True);q=ds.add_parser('generate');q.add_argument('--output',type=Path,default=Path('datasets'));q.add_argument('--seed',type=int,default=20261111);q.add_argument('--suite',choices=['baseline','multilingual'],default='baseline',help='Original 40 documents or 100 English/Chinese/mixed regression documents');q.add_argument('--json',action='store_true')
    q=sub.add_parser('benchmark');q.add_argument('input',type=Path);q.add_argument('--output',type=Path,default=Path('benchmark-results'));q.add_argument('--concurrency',type=int,default=2);q.add_argument('--json',action='store_true')
    return p

def run(args):
    if args.command=='datasets':
        if args.suite=="multilingual":
            from .multilingual_dataset import generate
        else:
            from .datasets import generate
        return generate(args.output,args.seed),0
    if args.command=='benchmark':
        from .benchmark import benchmark
        return benchmark(args.input,args.output,args.concurrency),0
    c=Client(args.server)
    if args.command=='status': return c.request('GET','/api/jobs/'+args.job_id).json(),0
    if args.command=='report': return c.request('GET',f'/api/jobs/{args.job_id}/report').json(),0
    if args.command=='delete': c.request('DELETE','/api/jobs/'+args.job_id); return {'id':args.job_id,'deleted':True},0
    if args.command=='download': return {'id':args.job_id,'paths':c.download(args.job_id,args.output)},0
    src=args.input.resolve(); output=args.output.resolve()
    if not src.exists(): raise FileNotFoundError('Input does not exist')
    if src.is_dir() and (output==src or src in output.parents): raise ValueError('Output directory must be outside the input tree')
    files=sorted((src.rglob('*') if args.recursive else src.glob('*'))) if src.is_dir() else [src]
    files=[f for f in files if f.is_file() and (not src.is_dir() or f.suffix.lower() in config.SUPPORTED|{'.doc','.ppt','.xls'})]
    if not files: raise ValueError('No supported documents found')
    results=[]; code=0
    for f in files:
        try:
            if f.suffix.lower() not in config.SUPPORTED: raise ValueError('Legacy/unsupported format: convert to a supported format first')
            if f.stat().st_size>config.MAX_BYTES: raise ValueError('File size limit exceeded')
            # Opaque job folder under the original relative parent avoids collisions.
            jid=c.submit(f)
            result={'input':str(f),'id':jid,'status':'queued'}
            if not args.async_:
                job=c.wait(jid,quiet=args.json);result['status']=job['status']
                parent=f.relative_to(src).parent if src.is_dir() else Path()
                target=output/parent/jid
                result['paths']=c.download(jid,target)
                if job['status'] in {'review_required','blocked'} and code==0: code=2
                elif job['status']=='failed': code=1 if code==0 else code
            results.append(result)
        except (httpx.HTTPError,OSError,ValueError,TimeoutError) as e:
            results.append({'input':str(f),'status':'failed','error':str(e)});code=1
    return {'jobs':results},code

def main():
    p=parser()
    try: args=p.parse_args()
    except SystemExit as e: raise SystemExit(64 if e.code else 0)
    try:
        result,code=run(args)
        if getattr(args,'json',False): print(json.dumps(result,ensure_ascii=False))
        else: print(json.dumps(result,indent=2,ensure_ascii=False))
        raise SystemExit(code)
    except (httpx.HTTPError,OSError,ValueError,TimeoutError) as e:
        if getattr(args,'json',False): print(json.dumps({'status':'failed','error':str(e)}))
        else: print('veil:',str(e),file=sys.stderr)
        raise SystemExit(1)
if __name__=='__main__':main()
