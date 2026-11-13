"""Cached synthetic compatibility checks and a shared provider circuit breaker.

Only capability flags, safe issue codes and timestamps are persisted. Requests
used for readiness contain synthetic examples, never uploaded document text.
"""
import hashlib
import json
import sqlite3
import time
import uuid

from . import config, store
from .llm import LLMError, LLMSettings, ProviderError, PROMPT_VERSION
from .provider_adapter import ChatCapabilities

MESSAGES = {
    'checking': 'Checking model compatibility before processing.',
    'configuration': 'Model setup needs attention. Retrying automatically.',
    'authentication': 'Model access is unavailable. Ask your administrator; retries continue.',
    'rate_limit': 'The model is busy. Retrying automatically.',
    'invalid_response': 'Checking model responses before retrying automatically.',
    'timeout': 'The model is slow to respond. Retrying automatically.',
    'connection': 'The model is unreachable. Retrying automatically.',
    'queue_timeout': 'The model is busy. Retrying automatically.',
    'unavailable': 'The model is temporarily unavailable. Retrying automatically.',
}


def provider_key(settings):
    identity = dict(endpoint=settings.endpoint, model=settings.model,
                    credential=hashlib.sha256(settings.api_key.encode()).hexdigest(),
                    enabled=settings.allow_network, input=settings.input_token_budget,
                    output=settings.max_output_tokens, contract=PROMPT_VERSION, probe_version=2)
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _row(key):
    with store.db() as db:
        row = db.execute('SELECT * FROM provider_state WHERE provider_key=?', (key,)).fetchone()
    return dict(row) if row else None


def public_status(settings=None):
    try:
        settings = settings or LLMSettings.from_env()
        if not (config.DATA/'jobs.sqlite3').is_file():
            return {'status': 'unknown', 'message': 'Model compatibility will be checked before processing.'}
        row = _row(provider_key(settings))
    except (ValueError, OSError, sqlite3.Error):
        return {'status': 'unknown', 'message': 'Model compatibility has not been checked.'}
    if not row:
        return {'status': 'unknown', 'message': 'Model compatibility will be checked before processing.'}
    status = row['status']
    if status == 'ready' and row['expires'] <= time.time():
        status = 'unknown'
    return {'status': status, 'last_checked': row['checked'] or None,
            'retry_at': row['next_probe'] or None,
            'message': row['message'] or ('Model compatibility checked.' if status == 'ready' else 'Model compatibility will be checked before processing.')}


class ProviderMonitor:
    def __init__(self, settings):
        self.settings = settings
        self.key = provider_key(settings)
        self.owner = uuid.uuid4().hex
        self.profile_checked = 0

    def ensure_ready(self, detector):
        now = time.time()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM provider_state WHERE provider_key=?', (self.key,)).fetchone()
            if row and row['status'] == 'ready' and row['expires'] > now:
                detector.capabilities = ChatCapabilities.restore(json.loads(row['capabilities'] or '{}'))
                self.profile_checked = row['checked']
                return True
            if row and ((row['status'] == 'checking' and row['lease_until'] > now) or row['next_probe'] > now):
                return False
            lease = now + self.settings.timeout*3 + 30
            if row:
                db.execute("UPDATE provider_state SET status='checking',owner=?,lease_until=?,message=?,code='checking' WHERE provider_key=?",
                           (self.owner, lease, MESSAGES['checking'], self.key))
            else:
                db.execute("""INSERT INTO provider_state(provider_key,status,owner,lease_until,message,code)
                              VALUES (?,'checking',?,?,?,'checking')""",
                           (self.key, self.owner, lease, MESSAGES['checking']))
        detector.capabilities = ChatCapabilities()
        detector.reset()
        try:
            detector.preflight()
        except ProviderError as error:
            self.fail(error.code, owner=self.owner)
            return False
        except LLMError:
            self.fail('invalid_response', owner=self.owner)
            return False
        except Exception:
            # Even unexpected client/parser failures must not publish output or
            # leak request contents through tracebacks or database messages.
            self.fail('unavailable', owner=self.owner)
            return False
        finally:
            detector.reset()
        now = time.time()
        with store.db() as db:
            updated = db.execute("""UPDATE provider_state SET status='ready',checked=?,expires=?,next_probe=0,
                         lease_until=0,owner=NULL,failures=0,code=NULL,message=NULL,capabilities=?
                         WHERE provider_key=? AND owner=?""",
                         (now, now+config.PROVIDER_CHECK_TTL,
                          json.dumps(detector.capabilities.public()), self.key, self.owner)).rowcount
        if updated:
            self.profile_checked = now
        return bool(updated)

    def fail(self, code, owner=None):
        code = code if code in MESSAGES else 'unavailable'
        now = time.time()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM provider_state WHERE provider_key=?', (self.key,)).fetchone()
            if row and owner is not None and row['owner'] != owner:
                return
            failures = min(10, (row['failures'] if row else 0)+1)
            delay = min(60, config.PROVIDER_RETRY_SECONDS * 2**(failures-1))
            if row:
                db.execute("""UPDATE provider_state SET status='waiting',checked=?,expires=0,next_probe=?,
                           lease_until=0,owner=NULL,failures=?,code=?,message=? WHERE provider_key=?""",
                           (now, now+delay, failures, code, MESSAGES[code], self.key))
            else:
                db.execute("""INSERT INTO provider_state(provider_key,status,checked,next_probe,failures,code,message)
                              VALUES (?,'waiting',?,?,?,?,?)""",
                           (self.key, now, now+delay, failures, code, MESSAGES[code]))

    def waiting(self):
        row = _row(self.key)
        if not row:
            return MESSAGES['checking'], 'checking', time.time()+config.PROVIDER_RETRY_SECONDS
        return (row['message'] or MESSAGES['checking'], row['code'] or 'checking',
                row['next_probe'] or row['lease_until'] or time.time()+config.PROVIDER_RETRY_SECONDS)

    def save_capabilities(self, detector):
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM provider_state WHERE provider_key=? AND status='ready' AND checked=?",
                             (self.key, self.profile_checked)).fetchone()
            if not row:
                return
            previous = ChatCapabilities.restore(json.loads(row['capabilities'] or '{}'))
            caps = detector.capabilities.public()
            caps['response_format'] = caps['response_format'] and previous.response_format
            caps['temperature'] = caps['temperature'] and previous.temperature
            db.execute('UPDATE provider_state SET capabilities=? WHERE provider_key=?',
                       (json.dumps(caps), self.key))
