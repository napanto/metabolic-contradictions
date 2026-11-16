"""Transactional OpenRouter accounting with legacy JSON audit exports.

No API calls. Migration requires signed authority and a drained JSON controller.
The model client/producer are unchanged and receive this ledger by injection.
"""
from __future__ import annotations

import ast
import fcntl
import gzip
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .io import digest, file_sha, utcnow
from .llm import AccountingError, BudgetError
from .spend_budget import PhaseLedger, ReservationCapacityWait, money
from .frozen_compat import _signed

AUTHORITY = 'configs/accounting_backend.json'
DATABASE = 'data/raw/v2/accounting/openrouter.sqlite3'
JSON_EXPORT = 'results/openrouter_spend.json'
PHASES = {'production_extraction', 'production_classification'}
RUNTIME = ('src/chl_rag/sqlite_ledger.py', 'scripts/run_v2_sqlite_production.py',
           'src/chl_rag/spend_budget.py', 'src/chl_rag/model_runs.py')
ARRAYS = {'calls', 'pending', 'unknown_cost_calls', 'account_credit_snapshot_history'}
SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE calls (seq INTEGER PRIMARY KEY, reservation_id TEXT UNIQUE NOT NULL,
 phase TEXT NOT NULL, request_hash TEXT NOT NULL, generation_identity TEXT NOT NULL,
 cost_decimal TEXT NOT NULL, row_json TEXT NOT NULL);
CREATE INDEX calls_request ON calls(request_hash,generation_identity);
CREATE TABLE pending (reservation_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
 request_hash TEXT NOT NULL, reserved_decimal TEXT NOT NULL, uncertain INTEGER NOT NULL,
 response_sha256 TEXT, row_json TEXT NOT NULL);
CREATE INDEX pending_request ON pending(request_hash,response_sha256);
CREATE TABLE unknown_events (seq INTEGER PRIMARY KEY, reservation_id TEXT, row_json TEXT NOT NULL);
CREATE TABLE credit_history (seq INTEGER PRIMARY KEY, row_json TEXT NOT NULL);
CREATE TABLE phase_stats (phase TEXT PRIMARY KEY, cost_decimal TEXT NOT NULL,
 pending_decimal TEXT NOT NULL, live_decimal TEXT NOT NULL, settled_count INTEGER NOT NULL,
 pending_count INTEGER NOT NULL, unknown_count INTEGER NOT NULL);
PRAGMA user_version=1;
'''


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _safe(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise AccountingError('Accounting path must be project-relative')
    result = root / relative
    if any(p.is_symlink() for p in (result, *result.parents)):
        raise AccountingError('Accounting paths must not traverse symlinks')
    return result


def authority(root):
    root = Path(root).resolve(); path = root / AUTHORITY
    if not path.is_file():raise AccountingError('Signed SQLite backend authority is required')
    document = json.loads(path.read_text())
    if (document.get('schema_version') != 1 or document.get('status') != 'registered'
            or document.get('backend') != 'sqlite' or document.get('vendor') != 'openrouter'
            or type(document.get('model_post_wall_deadline_seconds')) is not int
            or document.get('model_post_wall_deadline_seconds') != 480
            or document.get('database_path') != DATABASE or document.get('json_export_path') != JSON_EXPORT
            or not isinstance(document.get('source_json_sha256'), str)):
        raise AccountingError('Invalid SQLite backend authority')
    _signed(path, root)
    config = _safe(root, 'configs/evaluation_system.json')
    if file_sha(config) != document.get('evaluation_config_sha256'):
        raise AccountingError('SQLite authority binds another evaluation configuration')
    protocol = _safe(root, document['protocol_path'])
    if file_sha(protocol) != document.get('protocol_sha256'):
        raise AccountingError('SQLite authority protocol changed')
    _signed(protocol, root)
    for relative in RUNTIME:
        source = _safe(root, relative)
        if file_sha(source) != document.get('runtime_sha256', {}).get(relative):
            raise AccountingError('SQLite runtime changed after review')
        _signed(source, root)
    tree=ast.parse((root/'src/chl_rag/spend_budget.py').read_text())
    phase=next((node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='PhaseLedger'),None)
    constructor=next((node for node in phase.body if isinstance(node,ast.FunctionDef) and node.name=='__init__'),None) if phase else None
    if constructor is None or not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name)
            and node.func.id=='reject_legacy_json_writer' for node in ast.walk(constructor)):
        raise AccountingError('Activated legacy ledger lacks the persistent SQLite constructor guard')
    return document


def reject_legacy_json_writer(root):
    """Persistent staged constructor gate; a broken marker never enables fallback."""
    root = Path(root).resolve()
    if (root / AUTHORITY).exists():
        authority(root)
        raise AccountingError('OpenRouter accounting requires the signed SQLite runner; legacy JSON writer disabled')


def _meta(connection, key):
    row = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set(connection, key, value):
    connection.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, _encoded(value)))


def _decimal(connection, key):
    return Decimal(_meta(connection, key) or '0')


def _add(connection, key, change):
    _set(connection, key, str(_decimal(connection, key) + change))


def _phase(connection, name, *, cost=Decimal(0), pending=Decimal(0), live=Decimal(0), settled_count=0, pending_count=0, unknown_count=0):
    row = connection.execute('SELECT * FROM phase_stats WHERE phase=?', (name,)).fetchone()
    prior = row or (name, '0', '0', '0', 0, 0, 0)
    connection.execute('INSERT OR REPLACE INTO phase_stats VALUES (?,?,?,?,?,?,?)',
        (name, str(Decimal(prior[1])+cost), str(Decimal(prior[2])+pending), str(Decimal(prior[3])+live),
         prior[4]+settled_count, prior[5]+pending_count, prior[6]+unknown_count))


def _bump(connection, header):
    header['updated_at'] = utcnow()
    _set(connection, 'header', header)
    _set(connection, 'revision', (_meta(connection, 'revision') or 0)+1)


def _exclusive(path, stack):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = stack.enter_context(path.open('a'))
    try:fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:raise AccountingError('Accounting migration/run lock is busy') from None
    return stream


def _archive(raw, path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        with gzip.open(path, 'rb') as stream:
            if stream.read() != raw:raise AccountingError('Original ledger archive differs')
        return
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as target, gzip.GzipFile(filename='', mode='wb', fileobj=target, mtime=0) as zipped:
            zipped.write(raw)
        os.link(temporary, path)
    finally:Path(temporary).unlink(missing_ok=True)


def inspect_json(root=None):
    root = Path(root or Path.cwd()).resolve(); path = root / JSON_EXPORT
    raw = path.read_bytes(); value = json.loads(raw)
    if value.get('vendor') != 'openrouter':raise AccountingError('Wrong accounting vendor')
    live = {key: row for key, row in value['pending'].items() if not row.get('uncertain')}
    return {'source_json_sha256':hashlib.sha256(raw).hexdigest(), 'source_bytes':len(raw),
            'settled_calls':len(value['calls']), 'pending':len(value['pending']),
            'uncertain_pending':len(value['pending'])-len(live), 'potentially_live_HTTP':len(live),
            'unknown_cost_events':len(value.get('unknown_cost_calls', [])), 'total_cost':value['total_cost'],
            'API_calls':0, 'migration_performed':False}


def _migration_proof(root, connection):
    proof={'schema_version':1,'status':'migration_complete','database_id':_meta(connection,'database_id'),
           'source_json_sha256':_meta(connection,'source_json_sha256'),
           'authority_sha256':_meta(connection,'authority_sha256'),
           'original_archive':_meta(connection,'original_archive'),
           'original_archive_sha256':_meta(connection,'original_archive_sha256'),
           'imported_at':_meta(connection,'imported_at'),'counts':_meta(connection,'imported_counts'),
           'actual_HTTP_at_import':0,'API_calls':0,'original_ledger_bytes_preserved':True}
    if file_sha(_safe(root,proof['original_archive']))!=proof['original_archive_sha256']:
        raise AccountingError('Original accounting archive changed')
    path=root/'data/raw/v2/accounting/migration.json'
    data=(json.dumps(proof,indent=2)+'\n').encode()
    if path.exists():
        if path.read_bytes()!=data:raise AccountingError('Migration proof differs from the canonical database')
    else:
        with path.open('xb') as stream:stream.write(data)
        path.chmod(0o600)
    return proof


def migrate(root=None):
    """Import once, under old JSON and both production locks; never send a request."""
    root = Path(root or Path.cwd()).resolve(); approved = authority(root)
    db = _safe(root, DATABASE); source = _safe(root, JSON_EXPORT)
    with ExitStack() as stack:
        for relative in ('results/production_uncorrected/extraction.lock', 'results/production_uncorrected/classification_full.lock',
                         'results/production_uncorrected/classification_reserve.lock', 'results/openrouter_spend.lock'):
            _exclusive(_safe(root, relative), stack)
        if db.exists():
            connection = sqlite3.connect(db)
            try:
                if (_meta(connection, 'migration_complete') is not True
                        or _meta(connection, 'source_json_sha256') != approved['source_json_sha256']):
                    raise AccountingError('Existing SQLite store has another or incomplete migration')
                _migration_proof(root,connection)
                return {'status':'already_migrated', 'database_id':_meta(connection, 'database_id'), 'API_calls':0}
            finally:connection.close()
        raw = source.read_bytes(); value = json.loads(raw); original_sha = hashlib.sha256(raw).hexdigest()
        if original_sha != approved['source_json_sha256'] or value.get('vendor') != 'openrouter':
            raise AccountingError('Stopped JSON ledger differs from signed migration authority')
        if any(not row.get('uncertain') for row in value['pending'].values()):
            raise AccountingError('Actual HTTP reservations must drain before migration')
        source_total = sum((money(row['cost']) for row in value['calls']), Decimal(0))
        if float(source_total) != value['total_cost']:
            raise AccountingError('Source ledger total differs from its settled calls')
        archive = root / f'data/raw/v2/accounting/history/original_{original_sha}.json.gz'
        _archive(raw, archive)
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = db.with_name('.migration-'+uuid.uuid4().hex+'.sqlite3')
        connection = sqlite3.connect(temporary, isolation_level=None)
        imported=False
        try:
            temporary.chmod(0o600)
            connection.execute('PRAGMA journal_mode=WAL'); connection.execute('PRAGMA synchronous=FULL')
            connection.executescript(SCHEMA); connection.execute('BEGIN IMMEDIATE')
            header = {k:deep_value(v) for k,v in value.items() if k not in ARRAYS and k != 'total_cost'}
            for seq, row in enumerate(value['calls'], 1):
                connection.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?)', (seq, row['reservation_id'], row['phase'],
                    row['request_hash'], _encoded(row.get('response_metadata', {}).get('generation_id')),
                    str(money(row['cost'])), _encoded(row)))
                _phase(connection, row['phase'], cost=money(row['cost']), settled_count=1)
            pending_total = Decimal(0)
            for token, row in value['pending'].items():
                amount = money(row['reserved']); pending_total += amount
                connection.execute('INSERT INTO pending VALUES (?,?,?,?,?,?,?)', (token, row['phase'], row['request_hash'],
                    str(amount), int(bool(row.get('uncertain'))), row.get('observed_response_sha256'), _encoded(row)))
                _phase(connection, row['phase'], pending=amount, pending_count=1, unknown_count=1)
            connection.executemany('INSERT INTO unknown_events VALUES (?,?,?)',
                [(i, row.get('reservation_id'), _encoded(row)) for i,row in enumerate(value.get('unknown_cost_calls', []), 1)])
            connection.executemany('INSERT INTO credit_history VALUES (?,?)',
                [(i, _encoded(row)) for i,row in enumerate(value.get('account_credit_snapshot_history', []), 1)])
            database_id = uuid.uuid4().hex
            for key, data in {'header':header, 'total_cost_decimal':str(source_total), 'pending_decimal':str(pending_total),
                    'live_decimal':'0', 'revision':0, 'last_export_revision':0, 'last_export_sha256':original_sha,
                    'database_id':database_id, 'source_json_sha256':original_sha, 'authority_sha256':file_sha(root/AUTHORITY),
                    'migration_complete':True, 'original_archive':archive.relative_to(root).as_posix(),
                    'original_archive_sha256':file_sha(archive), 'imported_at':utcnow(),
                    'imported_counts':{'calls':len(value['calls']), 'pending':len(value['pending']),
                        'unknown_cost_calls':len(value.get('unknown_cost_calls', [])),
                        'credit_history':len(value.get('account_credit_snapshot_history', []))}}.items():
                _set(connection, key, data)
            connection.execute('COMMIT'); connection.execute('PRAGMA wal_checkpoint(TRUNCATE)');imported=True
        except BaseException:
            if connection.in_transaction:connection.execute('ROLLBACK')
            raise
        finally:
            connection.close()
            if not imported:temporary.unlink(missing_ok=True)
        try:
            if source.read_bytes() != raw:raise AccountingError('JSON ledger changed during locked migration')
            os.link(temporary, db)
        finally:temporary.unlink(missing_ok=True)
        proof_connection=sqlite3.connect(db)
        try:proof=_migration_proof(root,proof_connection)
        finally:proof_connection.close()
        return {**proof,'status':'migrated','ledger_bytes_unchanged':True}


def deep_value(value):
    return json.loads(_encoded(value))


class SQLitePhaseLedger:
    """Compatible transport-ledger API; all ordinary accounting operations are O(1)."""
    def __init__(self, vendor, phase, root=None):
        if vendor != 'openrouter' or phase not in PHASES:raise ValueError('SQLite ledger is production OpenRouter only')
        self.vendor, self.phase = vendor, phase
        self.root = Path(root or Path.cwd()).resolve()
        self.approved = authority(self.root)
        self.authority_sha256 = file_sha(self.root/AUTHORITY)
        self.database_path = _safe(self.root, DATABASE); self.path = _safe(self.root, JSON_EXPORT)
        if not self.database_path.is_file():raise AccountingError('Run the reviewed drained migration first')
        self.local = threading.local(); self.connections = []; self.connection_lock = threading.Lock()
        self.export_lock = threading.RLock(); self.lease = None; self.blocked = None
        connection = self._connection()
        if (connection.execute('PRAGMA user_version').fetchone()[0] != 1 or _meta(connection,'migration_complete') is not True
                or _meta(connection,'source_json_sha256') != self.approved['source_json_sha256']
                or _meta(connection,'authority_sha256') != self.authority_sha256):
            raise AccountingError('SQLite migration/authority identity mismatch')
        _migration_proof(self.root,connection)
        self._check_export(connection)

    def _connection(self):
        if not hasattr(self.local, 'connection'):
            connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None, check_same_thread=False)
            connection.execute('PRAGMA synchronous=FULL'); connection.execute('PRAGMA busy_timeout=30000')
            self.local.connection = connection
            with self.connection_lock:self.connections.append(connection)
        return self.local.connection

    @contextmanager
    def _transaction(self, *, write=True):
        connection = self._connection(); connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield connection
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:connection.execute('ROLLBACK')
            raise

    def _funding(self):
        if file_sha(self.root/AUTHORITY) != self.authority_sha256:
            raise AccountingError('SQLite backend authority changed during execution')
        document = json.loads((self.root/'configs/funding.json').read_text())
        if document.get('status') != 'confirmed' or not document.get('authority'):
            raise BudgetError('Funding requires explicit coordinator confirmation')
        funding = document['vendors'][self.vendor]
        if funding.get('headroom_policy') != 'real_balance_own_inflight_floor':
            raise BudgetError('SQLite production supports the registered real-balance funding policy only')
        return funding

    def funding(self):return self._funding()

    @staticmethod
    def _headroom(connection, header):
        snapshot = header.get('account_credit_snapshot')
        if snapshot is None:raise BudgetError('Fresh read-only credit snapshot required')
        settled_delta = max(Decimal(0), money(float(_decimal(connection,'total_cost_decimal')))-money(snapshot['own_cost_before_request']))
        return money(snapshot['real_balance'])-settled_delta-_decimal(connection,'live_decimal')

    def needs_credit_refresh(self, max_age_seconds=60):
        self._funding()
        with self._transaction(write=False) as connection:
            snapshot = _meta(connection,'header').get('account_credit_snapshot')
            return snapshot is None or (datetime.now(UTC)-datetime.fromisoformat(snapshot['checked_at'])).total_seconds()>max_age_seconds

    def record_credit_snapshot(self, *, total_credits, total_usage, own_cost_before_request, checked_at):
        credits, usage, own = money(total_credits), money(total_usage), money(own_cost_before_request)
        if usage>credits:raise BudgetError('Account balance is exhausted')
        with self._transaction() as connection:
            header=_meta(connection,'header');old=header.get('account_credit_snapshot')
            if old and (money(old['total_credits'])>credits or (money(old['total_credits'])==credits and money(old['total_usage'])>usage)):return
            row={'checked_at':checked_at,'total_credits':float(credits),'total_usage':float(usage),
                 'real_balance':float(credits-usage),'own_cost_before_request':float(own),
                 'authority':'INBOX03:38/04:15; real balance and current funding floor; uncertain liabilities remain audit-only'}
            header['account_credit_snapshot']=row
            connection.execute('INSERT INTO credit_history(row_json) VALUES (?)',(_encoded(row),))
            _bump(connection,header)

    def reserve(self, amount, request_hash, model, metadata=None):
        amount=money(amount)
        if amount<=0:raise ValueError('Reservation must be positive')
        if self.blocked:raise AccountingError(self.blocked)
        funding=self._funding()
        with self._transaction() as connection:
            header=_meta(connection,'header')
            if header.get('price_mismatch'):raise AccountingError('Actual cost exceeded reservation; review prices before spending')
            if funding.get('new_calls_paused') and model not in funding.get('comparison_stop_allow_models',[]):
                raise BudgetError('New calls explicitly paused pending coordinator confirmation')
            headroom=self._headroom(connection,header)
            if headroom-amount<money(funding['floor_usd']):
                live=_decimal(connection,'live_decimal')
                if live and headroom+live-amount>=money(funding['floor_usd']):
                    raise ReservationCapacityWait('Waiting for own in-flight reservations to settle')
                raise BudgetError('Real account headroom would fall below the configured funding floor')
            token=uuid.uuid4().hex
            recorded_amount=money(float(amount))
            row={'phase':self.phase,'reserved':float(amount),'request_hash':request_hash,'model':model,
                 'metadata':metadata or {},'started_at':utcnow(),'owner_pid':os.getpid()}
            connection.execute('INSERT INTO pending VALUES (?,?,?,?,0,NULL,?)',
                               (token,self.phase,request_hash,str(recorded_amount),_encoded(row)))
            _add(connection,'pending_decimal',recorded_amount);_add(connection,'live_decimal',recorded_amount)
            _phase(connection,self.phase,pending=recorded_amount,live=recorded_amount,pending_count=1)
            header.update(status='active',funding=funding);_bump(connection,header)
            return token

    def settle(self, token, actual_cost, usage=None, metadata=None):
        cost=money(actual_cost);exceeded=False
        with self._transaction() as connection:
            found=connection.execute('SELECT row_json FROM pending WHERE reservation_id=?',(token,)).fetchone()
            if found is None:raise KeyError(token)
            row=json.loads(found[0]);amount=money(row['reserved']);unknown=bool(row.get('uncertain'))
            exceeded=cost>amount+Decimal('0.000000001')
            recorded_cost=money(float(cost))
            completed={**row,'reservation_id':token,'finished_at':utcnow(),'cost':float(cost),
                       'usage':usage,'response_metadata':metadata or {}}
            connection.execute('DELETE FROM pending WHERE reservation_id=?',(token,))
            connection.execute('INSERT INTO calls(reservation_id,phase,request_hash,generation_identity,cost_decimal,row_json) VALUES (?,?,?,?,?,?)',
                (token,row['phase'],row['request_hash'],_encoded((metadata or {}).get('generation_id')),str(recorded_cost),_encoded(completed)))
            _add(connection,'total_cost_decimal',recorded_cost);_add(connection,'pending_decimal',-amount)
            if not unknown:_add(connection,'live_decimal',-amount)
            _phase(connection,row['phase'],cost=recorded_cost,pending=-amount,live=Decimal(0) if unknown else -amount,
                   settled_count=1,pending_count=-1,unknown_count=-int(unknown))
            header=_meta(connection,'header');header['price_mismatch']=bool(header.get('price_mismatch') or exceeded)
            header['status']='blocked_price_mismatch' if header['price_mismatch'] else 'active' if connection.execute('SELECT 1 FROM pending LIMIT 1').fetchone() else 'idle'
            _bump(connection,header)
        if exceeded:raise AccountingError('Actual cost exceeded reserved upper bound')

    def release(self, token, reason):
        if not isinstance(reason,str) or not reason.strip():raise ValueError('Known-unbilled release requires a reason')
        return self.settle(token,0,metadata={'known_unbilled':True,'reason':reason})

    def uncertain(self, token, reason, *, response=None):
        with self._transaction() as connection:
            found=connection.execute('SELECT row_json FROM pending WHERE reservation_id=?',(token,)).fetchone()
            if found is None:raise ValueError('Unknown reservation')
            pending=json.loads(found[0]);already=bool(pending.get('uncertain'))
            pending.update(uncertain=True,billing_status='unknown',http_in_flight=False,record_type='uncertain_request_liability')
            evidence={'reservation_id':token,'reason':reason,'recorded_at':utcnow()}
            if isinstance(response,dict) and response:
                pending['observed_response_sha256']=digest(response)
                evidence['observed_response_sha256']=pending['observed_response_sha256']
                evidence['response_metadata']={'generation_id':response.get('id'),'provider':response.get('provider')}
            connection.execute('UPDATE pending SET uncertain=1,response_sha256=?,row_json=? WHERE reservation_id=?',
                               (pending.get('observed_response_sha256'),_encoded(pending),token))
            connection.execute('INSERT INTO unknown_events(reservation_id,row_json) VALUES (?,?)',(token,_encoded(evidence)))
            if not already:
                amount=money(pending['reserved']);_add(connection,'live_decimal',-amount)
                _phase(connection,pending['phase'],live=-amount,unknown_count=1)
            stats=connection.execute('SELECT settled_count,unknown_count FROM phase_stats WHERE phase=?',(pending['phase'],)).fetchone()
            denominator=stats[0]+stats[1];fraction=stats[1]/denominator if denominator else None
            header=_meta(connection,'header')
            header.setdefault('unknown_billing_audit',{})[pending['phase']]={
                'unknown_requests':stats[1],'completed_accounted_or_unknown_requests':denominator,'fraction':fraction,
                'report_in_STATUS_above_five_percent':fraction is not None and fraction>.05,
                'continue_run':True,'authority':'INBOX2026-11-14 04:15'}
            header['status']='active_with_worst_case_liability';_bump(connection,header)

    def finish(self, token, response=None, *, known_free=False, error=None):
        if known_free:return self.release(token,error or 'explicit no-generation rejection')
        response=response or {};usage=response.get('usage')
        usage=usage if isinstance(usage,dict) else {};cost=usage.get('cost')
        if isinstance(cost,bool) or not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:
            self.uncertain(token,error or 'missing provider usage cost',response=response)
            return None
        return self.settle(token,cost,usage,{'generation_id':response.get('id'),'provider':response.get('provider'),'error':error})

    def accounted(self, request_hash, response):
        with self._transaction(write=False) as connection:
            settled=connection.execute('SELECT 1 FROM calls WHERE request_hash=? AND generation_identity=? LIMIT 1',
                                       (request_hash,_encoded(response.get('id')))).fetchone()
            unknown=connection.execute('SELECT 1 FROM pending WHERE request_hash=? AND uncertain=1 AND response_sha256=? LIMIT 1',
                                       (request_hash,digest(response))).fetchone()
            return bool(settled or unknown)

    def check_projection(self, remaining_cost):
        funding=self._funding()
        with self._transaction(write=False) as connection:
            if self._headroom(connection,_meta(connection,'header'))-money(remaining_cost)<money(funding['floor_usd']):
                raise BudgetError('Projected new work would breach the current real-balance floor')
        return float(money(remaining_cost))

    def _document(self, connection):
        value=_meta(connection,'header');value['funding']=self._funding()
        value['total_cost']=float(_decimal(connection,'total_cost_decimal'))
        value['calls']=[json.loads(r[0]) for r in connection.execute('SELECT row_json FROM calls ORDER BY seq')]
        value['pending']={token:json.loads(row) for token,row in connection.execute('SELECT reservation_id,row_json FROM pending ORDER BY rowid')}
        value['unknown_cost_calls']=[json.loads(r[0]) for r in connection.execute('SELECT row_json FROM unknown_events ORDER BY seq')]
        value['account_credit_snapshot_history']=[json.loads(r[0]) for r in connection.execute('SELECT row_json FROM credit_history ORDER BY seq')]
        if (sum((money(row['cost']) for row in value['calls']),Decimal(0))!=_decimal(connection,'total_cost_decimal')
                or sum((money(row['reserved']) for row in value['pending'].values()),Decimal(0))!=_decimal(connection,'pending_decimal')
                or PhaseLedger.live_reservations(value)!=_decimal(connection,'live_decimal')):
            raise AccountingError('SQLite incremental totals disagree with the preserved financial records')
        value['sqlite_backend']={'database_id':_meta(connection,'database_id'),'revision':_meta(connection,'revision'),
                                 'source_json_sha256':_meta(connection,'source_json_sha256'),'authority_sha256':self.authority_sha256,
                                 'database_path':DATABASE,'export_is_periodic':True}
        return value

    def snapshot(self):
        with self._transaction(write=False) as connection:value=self._document(connection)
        return {**value,'liability':float(PhaseLedger.liability(value)),
                'phase_liability':float(PhaseLedger.liability(value,self.phase)),
                'external_classifier_liability':float(PhaseLedger.external_liability(self))}

    def totals(self):
        """Cheap monitoring/accessor; no history reconstruction or JSON export."""
        with self._transaction(write=False) as connection:
            header=_meta(connection,'header')
            return {'total_cost':float(_decimal(connection,'total_cost_decimal')),
                    'pending_reserved':float(_decimal(connection,'pending_decimal')),
                    'live_reserved':float(_decimal(connection,'live_decimal')),
                    'bank_headroom':float(self._headroom(connection,header)),
                    'revision':_meta(connection,'revision'),'last_export_revision':_meta(connection,'last_export_revision'),
                    'unknown_billing_audit':header.get('unknown_billing_audit',{}),'price_mismatch':header.get('price_mismatch',False)}

    def _check_export(self, connection):
        intent=_meta(connection,'export_intent') or {}
        allowed={_meta(connection,'last_export_sha256'),intent.get('sha256')}
        if not self.path.is_file() or file_sha(self.path) not in allowed:
            self.blocked='Legacy JSON export changed outside the canonical SQLite ledger; review required'
            raise AccountingError(self.blocked)

    def _recover_export_intent(self, connection):
        self._check_export(connection)
        intent=_meta(connection,'export_intent')
        if intent:
            if file_sha(self.path)==intent['sha256']:
                _set(connection,'last_export_sha256',intent['sha256'])
                _set(connection,'last_export_revision',intent['revision'])
            _set(connection,'export_intent',None)

    @contextmanager
    def legacy_json_lease(self):
        if self.lease is not None:raise AccountingError('Legacy JSON lease is already held')
        with ExitStack() as stack:
            stream=_exclusive(self.path.with_suffix('.lock'),stack)
            with self._transaction() as connection:self._recover_export_intent(connection)
            self.lease=stream
            try:yield self
            finally:self.lease=None

    def export_json(self):
        with self.export_lock, ExitStack() as stack:
            if self.lease is None:_exclusive(self.path.with_suffix('.lock'),stack)
            with self._transaction() as connection:self._recover_export_intent(connection)
            with self._transaction(write=False) as connection:
                self._check_export(connection);value=self._document(connection)
                revision=_meta(connection,'revision')
            data=(json.dumps(value,indent=2)+'\n').encode()
            checksum=hashlib.sha256(data).hexdigest()
            with self._transaction() as connection:
                _set(connection,'export_intent',{'sha256':checksum,'revision':revision})
            descriptor,temporary=tempfile.mkstemp(dir=self.path.parent,suffix='.sqlite-export.tmp')
            try:
                with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
                Path(temporary).replace(self.path)
            finally:Path(temporary).unlink(missing_ok=True)
            with self._transaction() as connection:
                _set(connection,'last_export_sha256',checksum);_set(connection,'last_export_revision',revision)
                _set(connection,'export_intent',None)
            return {'export_sha256':checksum,'revision':revision,'bytes':len(data)}

    def close(self):
        with self.connection_lock:
            for connection in self.connections:connection.close()
            self.connections=[]
