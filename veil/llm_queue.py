"""Endpoint-wide inference slots shared by API and worker processes on this host."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
import time


@contextmanager
def provider_slot(endpoint, concurrency, timeout):
    from . import config
    folder = config.DATA / 'llm-slots'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Credentials and document contents never form part of a filename.
    name = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
    started = time.monotonic()
    acquired = None
    try:
        while acquired is None:
            for index in range(concurrency):
                fd = os.open(folder / f'{name}-{index}.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                except BaseException:
                    os.close(fd)
                    raise
                else:
                    acquired = fd
                    break
            if acquired is None:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('Model request queue timed out')
                time.sleep(min(0.05, max(0, timeout - (time.monotonic() - started))))
        yield time.monotonic() - started
    finally:
        if acquired is not None:
            os.close(acquired)  # Also releases the lock on process exit/crash.
