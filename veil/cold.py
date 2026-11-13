"""Fresh-process cold-start probe for benchmark; no network."""
import sys
from pathlib import Path
from .engine import Engine
if __name__=='__main__':
    e=Engine();r=e.process(Path(sys.argv[1]),Path(sys.argv[2]));print(r['status'])
