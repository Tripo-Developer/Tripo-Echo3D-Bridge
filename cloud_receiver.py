"""Local Tripo Bridge -> echo3D receiver. No credentials are saved or logged."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile
from urllib.parse import quote, quote_plus
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from echo3d_bridge.server import parse_message, MAX_MESSAGE_BYTES, MAX_FILE_BYTES, CHUNK_BYTES
from echo3d_bridge.settings import APP_DATA

API_URL = 'https://api.echo3D.com/upload'


class UploadUncertain(Exception):
    pass


class Ledger:
    """Persist the intent BEFORE sending. A crash cannot silently trigger a retry."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts (scope TEXT, delivery TEXT, sha TEXT, state TEXT, entry TEXT, PRIMARY KEY(scope,delivery))')
        self.db.commit()

    def get(self, scope, delivery):
        return self.db.execute('SELECT sha,state,entry FROM receipts WHERE scope=? AND delivery=?', (scope, delivery)).fetchone()

    def set(self, scope, delivery, sha, state, entry=''):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)', (scope, delivery, sha, state, entry))

    def close(self):
        self.db.close()


def validate_glb(path):
    size = path.stat().st_size
    with path.open('rb') as stream:
        head = stream.read(12)
        if len(head) != 12 or struct.unpack('<4sII', head) != (b'glTF', 2, size):
            raise ValueError('Not a GLB v2 file with a matching length header')
        offset, first = 12, True
        while offset < size:
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise ValueError('Truncated GLB chunk')
            length, kind = struct.unpack('<II', chunk)
            if length % 4 or offset + 8 + length > size or (first and kind != 0x4E4F534A):
                raise ValueError('Invalid GLB chunk layout')
            if first:
                obj = json.loads(stream.read(length))
                if not isinstance(obj, dict) or not isinstance(obj.get('asset'), dict) or obj['asset'].get('version') != '2.0':
                    raise ValueError('Missing GLB v2 asset metadata')
            else:
                stream.seek(length, 1)
            offset += 8 + length
            first = False
        if first or offset != size:
            raise ValueError('Missing or incomplete GLB JSON chunk')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def safe_error_detail(body, credentials):
    """Show bounded server diagnostics, never reflected authentication values."""
    sensitive = re.compile(r'(?:key|token|secret|password|authorization|cookie|email)', re.I)
    def clean(obj):
        if isinstance(obj, dict):
            return {k: '[REDACTED]' if sensitive.search(k) else clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj
    text = body.decode('utf-8', 'replace')
    try:
        text = json.dumps(clean(json.loads(text)), ensure_ascii=True)
    except (ValueError, TypeError):
        # Remove labeled credentials even when the server returns plain text/HTML.
        text = re.sub(r'(?i)((?:[a-z_]*key|token|secret|password|authorization|cookie|email)\s*[=:]\s*)[^\s<>&,;]+', r'\1[REDACTED]', text)
    for value in sorted((str(v) for v in credentials.values() if v), key=len, reverse=True):
        for form in {value, quote(value, safe=''), quote_plus(value), json.dumps(value)[1:-1]}:
            text = text.replace(form, '[REDACTED]')
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
    text = ' '.join(text.split())
    return text[:1200] + (' [truncated]' if len(text) > 1200 else '') if text else '(empty response body)'


def bridge_failure_message(exc, limit=900):
    """Bounded one-line failure reason for the Bridge transfer panel.

    Upload errors already carry redacted echo3D detail from safe_error_detail;
    the receiver terminal keeps the full text.
    """
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(exc))
    text = ' '.join(text.split())
    if not text:
        return 'Upload failed without a diagnostic; inspect receiver terminal'
    return text[:limit] + (' [truncated]' if len(text) > limit else '')


def upload(path, name, credentials, transport=None):
    """Fixed HTTPS destination, no redirects, no automatic POST retry, bounded response."""
    fields = dict(credentials, target_type='2', noProcessingWait='true', allowDuplicate='false')
    safe_name = re.sub(r'[^\w. -]', '_', name).lstrip(' .')[:160] or 'model.glb'
    if not safe_name.lower().endswith('.glb'):
        safe_name += '.glb'
    try:
        # Do not trust environment proxies with authentication keys.
        with httpx.Client(timeout=httpx.Timeout(240, connect=15), follow_redirects=False,
                          trust_env=False, transport=transport) as client, path.open('rb') as model:
            with client.stream('POST', API_URL, data=fields,
                               files={'file': (safe_name, model, 'model/gltf-binary')}) as response:
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > 1024 * 1024:
                        raise UploadUncertain('Response exceeded safety limit')
                # Show actual server diagnostics with reflected credentials redacted.
                if response.status_code != 200:
                    raise UploadUncertain(f'HTTP {response.status_code}; echo3D: {safe_error_detail(bytes(body), credentials)}; inspect the collection before retrying')
                try:
                    obj = json.loads(body)
                except (ValueError, UnicodeError):
                    raise UploadUncertain(f'No structured asset receipt; echo3D: {safe_error_detail(bytes(body), credentials)}; inspect the collection before retrying') from None
                if (not isinstance(obj, dict) or obj.get('error') or
                        not isinstance(obj.get('id'), str) or
                        not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', obj['id'])):
                    raise UploadUncertain(f'Response did not include an asset id; echo3D: {safe_error_detail(bytes(body), credentials)}; inspect the collection before retrying')
                # This proves acceptance, not completed processing, per noProcessingWait.
                return obj['id']
    except httpx.HTTPError:
        raise UploadUncertain('Network failure; the server may have accepted the upload. Inspect the collection before retrying') from None


class Receiver:
    def __init__(self, ledger, scope, credentials=None, dry_run=False, uploader=upload):
        self.ledger, self.scope, self.credentials = ledger, scope, credentials
        self.dry_run, self.uploader = dry_run, uploader
        self.current = None
        self.task = None

    async def send(self, ws, kind, **payload):
        await ws.send(json.dumps({'type': kind, 'payload': payload}))

    def cleanup(self):
        if self.current:
            self.current['stream'].close()
            self.current['path'].unlink(missing_ok=True)
            self.current = None

    async def finish(self, ws, transfer):
        delivery = transfer['id']
        try:
            sha = await asyncio.to_thread(validate_glb, transfer['path'])
            previous = self.ledger.get(self.scope, delivery)
            if previous and previous[0] != sha:
                raise UploadUncertain('Delivery id reused with different bytes; blocked')
            if self.dry_run:
                print(f'DRY RUN validated {transfer["size"]} bytes; SHA-256 {sha}. No cloud request. Bridge queue retained.', flush=True)
                await self.send(ws, 'import_complete', fileId=delivery, success=False, message='Dry run only; not uploaded')
                return
            if previous and previous[1] == 'accepted':
                entry = previous[2]
                print(f'Already accepted; skipping duplicate delivery. Entry {entry}', flush=True)
            elif previous:
                raise UploadUncertain(f'Delivery {delivery} has an unresolved prior attempt. Use receipts and resolve commands')
            else:
                self.ledger.set(self.scope, delivery, sha, 'uncertain')
                entry = await asyncio.to_thread(self.uploader, transfer['path'], transfer['name'], self.credentials)
                self.ledger.set(self.scope, delivery, sha, 'accepted', entry)
                print(f'ACCEPTED by echo3D. Entry {entry}. Processing may still be pending.', flush=True)
            await self.send(ws, 'import_complete', fileId=delivery, success=True,
                            entryId=entry, message='echo3D accepted the asset; processing may still be pending')
        except ConnectionClosed:
            print('Bridge disconnected. Any accepted receipt is retained; reconnect to acknowledge without re-uploading.', flush=True)
        except (ValueError, OSError, UploadUncertain) as exc:
            print(f'NOT CONFIRMED: {exc}', flush=True)
            try:
                await self.send(ws, 'import_complete', fileId=delivery, success=False, message=bridge_failure_message(exc))
            except ConnectionClosed:
                pass
        finally:
            transfer['path'].unlink(missing_ok=True)
            if self.current is transfer:
                self.current = None

    async def frame(self, ws, raw):
        msg, body = parse_message(raw)
        p = msg.get('payload', {})
        if msg.get('type') != 'file_transfer':
            if msg.get('type') == 'transfer_cancelled':
                print('Bridge rejected or cancelled this delivery. Any in-flight request is NOT retried automatically.', flush=True)
                # Do not cancel an in-flight HTTP thread or discard its receipt.
                if not self.task or self.task.done():
                    self.cleanup()
            elif msg.get('type') == 'error':
                raise ValueError('Bridge returned a protocol error')
            return
        if not isinstance(raw, bytes):
            raise ValueError('Expected binary frame')
        index, total, size = p.get('chunkIndex'), p.get('chunkTotal'), p.get('chunkSize')
        if any(type(x) is not int for x in (index, total, size)) or not 0 <= index < total <= 1024:
            raise ValueError('Invalid chunk sequence')
        if size != len(body) or not 0 < size <= CHUNK_BYTES or p.get('fileType') != 'glb':
            raise ValueError('Invalid chunk body')
        if not isinstance(p.get('fileId'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p['fileId']):
            raise ValueError('Invalid delivery id')
        if not isinstance(p.get('fileName'), str) or not 1 <= len(p['fileName']) <= 512:
            raise ValueError('Invalid model name')
        if self.current is None:
            if index != 0:
                raise ValueError('First chunk must be zero')
            stream = tempfile.NamedTemporaryFile(prefix='echo3d-upload-', suffix='.glb', delete=False)
            self.current = dict(id=p['fileId'], name=p['fileName'], total=total,
                                index=0, size=0, stream=stream, path=Path(stream.name))
        t = self.current
        if (t['id'], t['name'], t['total'], t['index']) != (p['fileId'], p['fileName'], total, index):
            raise ValueError('Unexpected or interleaved transfer')
        if t['size'] + size > MAX_FILE_BYTES:
            raise ValueError('Model exceeds bridge limit of 256 MiB')
        t['stream'].write(body)
        t['size'] += size
        t['index'] += 1
        if t['index'] == total:
            t['stream'].flush()
            os.fsync(t['stream'].fileno())
            t['stream'].close()
        await self.send(ws, 'file_transfer_ack', fileId=t['id'], fileIndex=index, success=True)
        if t['index'] == total:
            self.task = asyncio.create_task(self.finish(ws, t))

    async def run(self, url='ws://127.0.0.1:60710/echo3d'):
        try:
            async with connect(url, max_size=MAX_MESSAGE_BYTES, compression=None) as ws:
                await self.send(ws, 'handshake', protocolVersion='1.1.0', clientName='echo3D cloud receiver')
                msg, body = parse_message(await asyncio.wait_for(ws.recv(), 10))
                if body or msg.get('type') != 'handshake_ack' or msg.get('payload', {}).get('success') is not True:
                    raise ValueError('Bridge handshake rejected; close the demo receiver first')
                print('Connected. Waiting for queued or new Tripo models. Ctrl+C stops.', flush=True)
                async for raw in ws:
                    await self.frame(ws, raw)
        finally:
            # If the socket drops, finish recording any upload result before exiting.
            if self.task and not self.task.done():
                await asyncio.shield(self.task)
            elif self.task:
                await self.task
            self.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Validate transfers without contacting echo3D or consuming the bridge queue')
    parser.add_argument('--ledger', type=Path, default=APP_DATA / 'cloud-receipts.sqlite3')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('receipts', help='Show local receipts (no credentials)')
    resolve = sub.add_parser('resolve', help='Resolve an uncertain attempt AFTER checking the collection')
    resolve.add_argument('scope')
    resolve.add_argument('delivery')
    group = resolve.add_mutually_exclusive_group(required=True)
    group.add_argument('--accepted-entry', help='Existing asset id found in the collection')
    group.add_argument('--allow-retry', action='store_true', help='Only if you verified the asset was NOT created')
    args = parser.parse_args()
    ledger = Ledger(args.ledger)
    try:
        if args.command == 'receipts':
            for row in ledger.db.execute('SELECT scope,delivery,state,entry FROM receipts'):
                print(' | '.join(row))
            return
        if args.command == 'resolve':
            old = ledger.get(args.scope, args.delivery)
            if not old or old[1] != 'uncertain':
                parser.error('No uncertain receipt at that scope/delivery')
            if args.allow_retry:
                with ledger.db:
                    ledger.db.execute('DELETE FROM receipts WHERE scope=? AND delivery=?', (args.scope, args.delivery))
            else:
                ledger.set(args.scope, args.delivery, old[0], 'accepted', args.accepted_entry)
            print('Receipt updated. Restart the receiver to redeliver pending models.')
            return
        credentials = None
        scope = 'dry-run'
        if not args.dry_run:
            if not sys.stdin.isatty():
                parser.error('Live mode requires an interactive terminal for hidden credential prompts')
            print('Use a TEST collection. Every queued and new bridge model will upload until you stop.\nUploads use your collection storage/processing allowance. No credentials are saved.')
            key = getpass.getpass('Target collection key (hidden): ').strip()
            email = input('echo3D account email: ').strip()
            user_key = getpass.getpass('echo3D User Authentication Key (hidden): ').strip()
            sec_key = getpass.getpass('Collection Security Key, if enabled (hidden; otherwise Enter): ').strip()
            if not key or not email or not user_key:
                parser.error('Collection key, email and User Authentication Key are required')
            if input('Type UPLOAD to authorize this local session: ').strip() != 'UPLOAD':
                return
            credentials = dict(key=key, email=email, userKey=user_key)
            if sec_key:
                credentials['secKey'] = sec_key
            scope = hashlib.sha256(key.encode()).hexdigest()[:24]
            print(f'Target collection fingerprint: {scope}')
        asyncio.run(Receiver(ledger, scope, credentials, args.dry_run).run())
    finally:
        ledger.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Check receipts/collection before retrying any interrupted upload.')
    except (OSError, ValueError, ConnectionClosed):
        print('Receiver stopped. Confirm Bridge is running, demo receiver is closed, and inspect receipts before retrying.')
        raise SystemExit(1) from None
