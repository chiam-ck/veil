#!/usr/bin/env python3
"""Copy only reviewed source into a fresh directory for GitHub/release builds."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from release_files import files
from release_check import check,environment_secrets


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--secret-env',type=Path)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1];destination=args.destination.resolve()
    if destination.exists():raise SystemExit('Destination already exists; refusing to overwrite it')
    paths=[p.relative_to(root) for p in files(root)]
    failures=check(root,paths,environment_secrets(args.secret_env))
    if failures:
        print(json.dumps({'passed':False,'findings':[{'path':p,'rule':r} for p,r in failures]},indent=2));return 1
    destination.mkdir(parents=True)
    for relative in paths:
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(root/relative,target)
        if (root/relative).stat().st_mode & 0o111:target.chmod(0o755)
    print(json.dumps({'passed':True,'source_files':len(paths),'destination':str(destination)}))
    return 0

if __name__=='__main__':sys.exit(main())
