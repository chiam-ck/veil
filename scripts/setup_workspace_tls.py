#!/usr/bin/env python3
"""Create Veil's private HTTPS CA/leaf without exposing private key contents."""
import argparse
import ipaddress
import os
from pathlib import Path
import subprocess
import tempfile


def run(*args):
    subprocess.run(['openssl',*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,default=Path('.veil/tls'))
    parser.add_argument('--host',action='append',default=[])
    parser.add_argument('--ca-private-directory',type=Path,default=Path('.veil-ca'))
    args=parser.parse_args()
    os.umask(0o077)
    folder=args.directory.resolve();folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    names=list(dict.fromkeys(['localhost','127.0.0.1','::1','veil-api']+args.host))
    sans=[]
    for name in names:
        try:ipaddress.ip_address(name);prefix='IP'
        except ValueError:
            if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in name):
                raise SystemExit('Use valid IP addresses or DNS hostnames')
            prefix='DNS'
        sans.append(prefix+':'+name)
    private=args.ca_private_directory.resolve();private.mkdir(parents=True,exist_ok=True,mode=0o700)
    ca_key=private/'ca.key';ca=folder/'ca.crt'
    if not ca_key.exists() and not ca.exists():
        run('req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','3650',
            '-subj','/CN=Veil Private Workspace CA','-addext','basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext','keyUsage=critical,keyCertSign,cRLSign','-keyout',str(ca_key),'-out',str(ca))
    elif not(ca_key.is_file() and ca.is_file()):raise SystemExit('Incomplete existing CA; refusing to replace it')
    # Never replace an installed server identity without an explicit new directory.
    if (folder/'server.key').exists() or (folder/'server.crt').exists():
        raise SystemExit('Server identity already exists; refusing to overwrite it')
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        temp=Path(temp);key=temp/'server.key';csr=temp/'server.csr';leaf=temp/'leaf.crt';extension=temp/'extensions.cnf'
        extension.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName='+','.join(sans)+'\n')
        run('req','-new','-newkey','rsa:3072','-nodes','-sha256','-subj','/CN=Veil Workspace',
            '-keyout',str(key),'-out',str(csr))
        run('x509','-req','-in',str(csr),'-CA',str(ca),'-CAkey',str(ca_key),'-set_serial',
            str(int.from_bytes(os.urandom(16),'big')),'-days','365','-sha256','-extfile',str(extension),'-out',str(leaf))
        (folder/'server.crt').write_bytes(leaf.read_bytes()+ca.read_bytes())
        key.rename(folder/'server.key')
    os.chmod(ca_key,0o600);os.chmod(folder/'server.key',0o600)
    for name in ['ca.crt','server.crt']:os.chmod(folder/name,0o644)
    print('Created private HTTPS identity. Trust the public ca.crt on client devices; keep both .key files private.')

if __name__=='__main__':main()
