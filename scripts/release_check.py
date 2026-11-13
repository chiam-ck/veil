#!/usr/bin/env python3
"""Fail closed on release file/secret mistakes. Never print matched values."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from release_files import allowed,files

SECRET_ENV = re.compile(r'(?:API_?KEY|PASSWORD|TOKEN|SECRET|PRIVATE_?KEY)',re.I)
# Output contains paths and rule names only, including on a failure.
PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'github-token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'aws-access-key': re.compile(rb'(?:AKIA|ASIA)[A-Z0-9]{16}'),
    'provider-token': re.compile(rb'\bsk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_-]{24,}'),
    'credential-url': re.compile(rb'https?://[^\s/\x22\x27]+:[^\s/\x22\x27]+@'),
    'local-home-path': re.compile(rb'/home/[A-Za-z0-9_.-]+/'),
}


def secret_name(name):
    return bool(SECRET_ENV.search(name)) and not name.endswith(('_TOKEN_BUDGET','_TOKENS'))


def environment_secrets(path=None):
    found=[]
    sources=[dict(os.environ)]
    if path and Path(path).is_file():
        values={}
        for line in Path(path).read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):continue
            name,value=line.split('=',1);values[name.strip()]=value.strip().strip('\"\'')
        sources.append(values)
    for values in sources:
        for name,value in values.items():
            if secret_name(name) and len(value)>=12 and not value.startswith('${'):
                found.append(value.encode())
    return found


def check(root,paths,secrets=()):
    root=Path(root).resolve();failures=[]
    for relative in paths:
        path=root/relative
        if not allowed(relative):failures.append((str(relative),'not-in-source-allowlist'));continue
        if path.is_symlink() or root not in path.resolve().parents:
            failures.append((str(relative),'symlink-or-external-path'));continue
        if not path.is_file():failures.append((str(relative),'missing-file'));continue
        data=path.read_bytes()
        if any(secret in data for secret in secrets):failures.append((str(relative),'runtime-secret-match'))
        for rule,pattern in PATTERNS.items():
            if pattern.search(data):failures.append((str(relative),rule))
    return failures


def check_index(root,secrets=()):
    """Read committed/staged blobs, so editing the worktree cannot hide a secret."""
    result=subprocess.run(['git','-C',str(root),'ls-files','--stage','-z'],check=True,capture_output=True)
    paths=[];failures=[]
    for entry in result.stdout.split(b'\0'):
        if not entry:continue
        metadata,name=entry.split(b'\t',1);mode,sha,stage=metadata.decode().split()
        relative=Path(os.fsdecode(name));paths.append(relative)
        if not allowed(relative):failures.append((str(relative),'not-in-source-allowlist'));continue
        if mode not in {'100644','100755'} or stage!='0':
            failures.append((str(relative),'symlink-submodule-or-unmerged-index'));continue
        data=subprocess.run(['git','-C',str(root),'cat-file','blob',sha],check=True,capture_output=True).stdout
        if any(secret in data for secret in secrets):failures.append((str(relative),'runtime-secret-match'))
        for rule,pattern in PATTERNS.items():
            if pattern.search(data):failures.append((str(relative),rule))
    return paths,failures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tracked',action='store_true',help='Inspect every Git-tracked path, not just the source allowlist')
    parser.add_argument('--secret-env',type=Path,help='Compare against private dotenv values in memory; values are never emitted')
    args=parser.parse_args();root=args.root.resolve()
    secrets=environment_secrets(args.secret_env)
    if args.tracked:paths,failures=check_index(root,secrets)
    else:
        paths=[p.relative_to(root) for p in files(root)]
        failures=check(root,paths,secrets)
    required={'.env.example','.gitignore','LICENSE','SECURITY.md','README.md','requirements.lock','requirements-build.lock','requirements-dev.lock'}
    for missing in required-{str(p) for p in paths}:failures.append((missing,'required-release-file-missing'))
    for lock in ['requirements.lock','requirements-build.lock','requirements-dev.lock']:
        path=root/lock
        if path.exists():
            text=path.read_text()
            if '--hash=sha256:' not in text or '--index-url' in text or '--extra-index-url' in text:
                failures.append((lock,'unhashed-or-index-specific-lock'))
    example=root/'.env.example'
    if example.exists():
        for line in example.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                name,value=line.split('=',1)
                if secret_name(name) and value.strip():failures.append(('.env.example','nonempty-secret-example'))
    # A filename itself can contain a credential; redact it before any output.
    findings=[]
    for path,rule in failures:
        data=path.encode()
        for secret in secrets:data=data.replace(secret,b'[REDACTED]')
        for pattern in PATTERNS.values():data=pattern.sub(b'[REDACTED]',data)
        findings.append({'path':data.decode(errors='replace'),'rule':rule})
    print(json.dumps({'passed':not failures,'files_checked':len(paths),'findings':findings},indent=2))
    return int(bool(failures))

if __name__=='__main__':sys.exit(main())
