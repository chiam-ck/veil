"""Anonymous browser credentials: opaque random cookies, server-side ownership.

Only token digests are stored. Unknown/expired cookies never claim existing data.
"""
import hashlib
import ipaddress
import re
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from . import config, store

COOKIE = '__Host-veil-workspace'
LOCAL_COOKIE = 'veil-local-workspace'
TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
IDLE_SECONDS = 24 * 3600
ABSOLUTE_SECONDS = 7 * 24 * 3600


def local_http(request):
    """Development only: HTTP on an actual loopback connection and hostname."""
    try:
        peer = request.client.host if request.client else ''
        host = request.url.hostname
        return (request.url.scheme == 'http' and ipaddress.ip_address(peer).is_loopback
                and (host == 'localhost' or ipaddress.ip_address(host).is_loopback))
    except ValueError:
        return False


def cookie_name(request):
    return LOCAL_COOKIE if local_http(request) else COOKIE


def require_transport(request):
    if request.url.scheme != 'https' and not local_http(request):
        raise HTTPException(426, 'Open Veil over HTTPS to use a private workspace.')


def resolve(request, touch=True):
    token = request.cookies.get(cookie_name(request), '')
    if not TOKEN.fullmatch(token):return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with store.db() as db:
        row = db.execute('SELECT * FROM workspaces WHERE token_hash=? AND expires>? AND absolute_expires>?',
                         (digest,now,now)).fetchone()
        if not row:return None
        result = dict(row)
        if touch and now-row['touched'] >= 60:
            result['expires'] = min(now+IDLE_SECONDS,row['absolute_expires'])
            db.execute('UPDATE workspaces SET touched=?,expires=? WHERE id=?',
                       (now,result['expires'],row['id']))
            request.state.refresh_workspace_cookie = (token,result['expires'])
    return result


def owner(request:Request):
    require_transport(request)
    row = resolve(request)
    if not row:
        raise HTTPException(401, 'This private workspace is missing or expired. Reload to start a new workspace; previous links do not restore access.')
    return row['id']


def bootstrap(request, response):
    require_transport(request)
    row = resolve(request)
    if not row:
        token=secrets.token_urlsafe(32);now=time.time()
        row=dict(id=uuid.uuid4().hex,expires=now+IDLE_SECONDS,absolute_expires=now+ABSOLUTE_SECONDS)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM workspaces WHERE expires<=? OR absolute_expires<=?',(now,now))
            peer=request.client.host if request.client else 'unknown'
            peer_hash=hashlib.sha256(peer.encode()).hexdigest()
            limit=db.execute('SELECT * FROM workspace_limits WHERE peer_hash=?',(peer_hash,)).fetchone()
            if limit and now-limit['started']<3600 and limit['attempts']>=config.WORKSPACE_CREATIONS_PER_HOUR:
                raise HTTPException(429,'Too many new workspaces. Reuse an existing browser workspace or retry later.',headers={'Retry-After':'3600'})
            if db.execute('SELECT count(*) FROM workspaces').fetchone()[0]>=config.MAX_WORKSPACES:
                raise HTTPException(429,'Workspace capacity reached; retry later.',headers={'Retry-After':'60'})
            if limit and now-limit['started']<3600:
                db.execute('UPDATE workspace_limits SET attempts=attempts+1 WHERE peer_hash=?',(peer_hash,))
            else:
                db.execute('INSERT OR REPLACE INTO workspace_limits VALUES (?,?,1)',(peer_hash,now))
            db.execute('INSERT INTO workspaces(id,token_hash,created,touched,expires,absolute_expires) VALUES (?,?,?,?,?,?)',
                       (row['id'],hashlib.sha256(token.encode()).hexdigest(),now,now,row['expires'],row['absolute_expires']))
        set_cookie(request,response,token,row['expires'])
    return dict(mode='anonymous-private',idle_timeout_hours=IDLE_SECONDS/3600,
                maximum_lifetime_hours=ABSOLUTE_SECONDS/3600,expires_at=row['expires'])


def set_cookie(request,response,token,expires):
    response.set_cookie(cookie_name(request),token,max_age=max(0,int(expires-time.time())),
                        secure=not local_http(request),httponly=True,samesite='strict',path='/')


def end(request,response,workspace_id):
    with store.db() as db:db.execute('DELETE FROM workspaces WHERE id=?',(workspace_id,))
    response.delete_cookie(cookie_name(request),secure=not local_http(request),httponly=True,samesite='strict',path='/')
