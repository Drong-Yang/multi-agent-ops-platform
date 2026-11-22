"""Synchronous SQLAlchemy persistence, usable through asyncio.to_thread.

Root repositories hold an Engine, never a Session/Connection. The repository
passed to an idempotency callback is transaction-bound and must not escape it.
SQLite is an offline contract backend; PostgreSQL is the application backend.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable
import uuid
from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, MetaData,
                        String, Table, create_engine, event, select, update)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool


class RepositoryError(Exception):
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code or self.__class__.__name__.lower()


class NotFound(RepositoryError):
    status_code = 404


class Conflict(RepositoryError):
    status_code = 409


class Invalid(RepositoryError):
    status_code = 422


metadata = MetaData()
records = Table(
    "fusion_records", metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(64), nullable=False, index=True),
    Column("owner_id", String(128), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("state_version", Integer, nullable=False, default=1),
    Column("source_id", String(64), ForeignKey("fusion_records.id")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
operations = Table(
    "fusion_operations", metadata,
    Column("owner_id", String(128), primary_key=True),
    Column("action", String(128), primary_key=True),
    Column("operation_key", String(200), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
knowledge_pointer = Table(
    "fusion_knowledge_pointer", metadata,
    Column("name", String(32), primary_key=True),
    Column("snapshot_id", String(64), ForeignKey("fusion_records.id")),
    Column("state_version", Integer, nullable=False, default=1),
)
_UNSET = object()
_ASSETS = ("asset", "content_asset", "faq")
_SNAPSHOTS = ("knowledge_snapshot", "snapshot")


def _now():
    return datetime.now(timezone.utc)


def _identity(value, label, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid(f"{label} must be a nonempty string of at most {limit} characters")
    return value


def _json(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise Invalid("Value must be finite JSON data") from exc


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, url_or_engine: str | Engine, *, _connection: Connection | None = None):
        self._connection = _connection
        self._owns_engine = isinstance(url_or_engine, str)
        if self._owns_engine:
            options = {"pool_pre_ping": True}
            if url_or_engine.startswith("sqlite"):
                options["connect_args"] = {"check_same_thread": False, "timeout": 30}
                if url_or_engine.endswith(":memory:") or url_or_engine in ("sqlite://", "sqlite+pysqlite://"):
                    options["poolclass"] = StaticPool
            self.engine = create_engine(url_or_engine, **options)
        else:
            self.engine = url_or_engine
        if self._owns_engine and self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_foreign_keys(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            yield self._connection
            return
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "sqlite":
                # Deterministic offline concurrency contracts; PostgreSQL uses
                # pointer row locks and atomic CAS instead of a database lock.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            else:
                with connection.begin():
                    yield connection

    def _bound(self, connection):
        return Repository(self.engine, _connection=connection)

    def initialize(self):
        metadata.create_all(self.engine)
        with self._transaction() as connection:
            if connection.execute(select(knowledge_pointer.c.name).where(
                    knowledge_pointer.c.name == "active")).first() is None:
                connection.execute(knowledge_pointer.insert().values(
                    name="active", snapshot_id=None, state_version=1))

    def close(self):
        if self._owns_engine:
            self.engine.dispose()

    @staticmethod
    def _record(row):
        result = dict(row)
        result["payload"] = _json(result["payload"])
        for key in ("created_at", "updated_at"):
            value = result[key]
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            result[key] = value.isoformat()
        return result

    def create(self, kind: str, owner_id: str, payload: dict, *, record_id=None, source_id=None):
        _identity(kind, "kind", 64)
        _identity(owner_id, "owner_id")
        record_id = _identity(record_id or uuid.uuid4().hex, "record_id", 64)
        if not isinstance(payload, dict):
            raise Invalid("payload must be a JSON object")
        payload, now = _json(payload), _now()
        with self._transaction() as connection:
            tx = self._bound(connection)
            if source_id is not None:
                tx.get(source_id, owner_id)  # A FK is not authorization.
            try:
                connection.execute(records.insert().values(
                    id=record_id, kind=kind, owner_id=owner_id, payload=payload,
                    state_version=1, source_id=source_id, created_at=now, updated_at=now))
            except IntegrityError as exc:
                raise Conflict("Record already exists or source reference is invalid") from exc
            return tx.get(record_id, owner_id)

    def get(self, record_id: str, owner_id: str, kind: str | None = None):
        _identity(record_id, "record_id", 64)
        _identity(owner_id, "owner_id")
        statement = select(records).where(records.c.id == record_id, records.c.owner_id == owner_id)
        if kind is not None:
            statement = statement.where(records.c.kind == kind)
        with self._transaction() as connection:
            row = connection.execute(statement).mappings().first()
            if row is None:
                raise NotFound("Record not found")
            return self._record(row)

    def list(self, kind: str, owner_id: str):
        _identity(kind, "kind", 64)
        _identity(owner_id, "owner_id")
        with self._transaction() as connection:
            return [self._record(row) for row in connection.execute(select(records).where(
                records.c.kind == kind, records.c.owner_id == owner_id).order_by(
                    records.c.created_at, records.c.id)).mappings()]

    def scan(self, kind: str):
        """Internal startup/runner scan. Never expose through a user API.

        Application endpoints must use owner-scoped get/list. This method is for
        recovering persisted run state and claiming queued runs after startup.
        """
        _identity(kind, "kind", 64)
        with self._transaction() as connection:
            return [self._record(row) for row in connection.execute(select(records).where(
                records.c.kind == kind).order_by(records.c.created_at, records.c.id)).mappings()]

    def update_cas(self, record_id: str, owner_id: str, expected_version: int, payload: dict):
        if type(expected_version) is not int or expected_version < 1:
            raise Invalid("expected_version must be a positive integer")
        if not isinstance(payload, dict):
            raise Invalid("payload must be a JSON object")
        payload = _json(payload)
        with self._transaction() as connection:
            tx = self._bound(connection)
            current = tx.get(record_id, owner_id)
            if (current["kind"] in _SNAPSHOTS and current["payload"].get("status") == "ready"
                    and current["payload"] != payload):
                raise Conflict("Ready knowledge snapshots are immutable; create a new snapshot")
            result = connection.execute(update(records).where(
                records.c.id == record_id, records.c.owner_id == owner_id,
                records.c.state_version == expected_version).values(
                    payload=payload, state_version=expected_version + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise Conflict("Record changed; reload before retrying", code="state_version_conflict")
            return tx.get(record_id, owner_id)

    def lookup_operation(self, owner_id: str, action: str, key: str, request: Any):
        """Replay a committed operation before any fallible external work.

        None means there is no operation in this owner's/action's namespace.
        A mismatching request must fail even on this early read path. No callback
        runs and no record is inserted or updated here.
        """
        _identity(owner_id, "owner_id")
        _identity(action, "action")
        _identity(key, "idempotency key", 200)
        request_hash = content_hash(json.dumps(_json(request), ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")))
        with self._transaction() as connection:
            row = connection.execute(select(operations).where(
                operations.c.owner_id == owner_id, operations.c.action == action,
                operations.c.operation_key == key)).mappings().first()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise Conflict("Idempotency key was used with different content")
            return _json(row["response"])

    def idempotent(self, owner_id: str, action: str, key: str, request: Any,
                   callback: Callable[[Repository], Any]):
        _identity(owner_id, "owner_id")
        _identity(action, "action")
        _identity(key, "idempotency key", 200)
        request_hash = content_hash(json.dumps(_json(request), ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")))
        criteria = (operations.c.owner_id == owner_id, operations.c.action == action,
                    operations.c.operation_key == key)
        try:
            with self._transaction() as connection:
                row = connection.execute(select(operations).where(*criteria)).mappings().first()
                if row is not None:
                    if row["request_hash"] != request_hash:
                        raise Conflict("Idempotency key was used with different content")
                    return _json(row["response"])
                connection.execute(operations.insert().values(
                    owner_id=owner_id, action=action, operation_key=key,
                    request_hash=request_hash, response=None, created_at=_now()))
                response = _json(callback(self._bound(connection)))
                connection.execute(update(operations).where(*criteria).values(response=response))
                return response
        except IntegrityError:
            # A PostgreSQL concurrent unique insert waits for the winner. Read
            # its committed result after the losing transaction has rolled back.
            with self._transaction() as connection:
                row = connection.execute(select(operations).where(*criteria)).mappings().first()
                if row is None:
                    raise Conflict("Concurrent operation failed; retry with the same key")
                if row["request_hash"] != request_hash:
                    raise Conflict("Idempotency key was used with different content")
                return _json(row["response"])

    @staticmethod
    def _pointer(connection, lock=False):
        statement = select(knowledge_pointer).where(knowledge_pointer.c.name == "active")
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise Conflict("Repository has not been initialized")
        return row

    def get_active_snapshot(self):
        with self._transaction() as connection:
            pointer = self._pointer(connection)
            if pointer["snapshot_id"] is None:
                return None
            row = connection.execute(select(records).where(
                records.c.id == pointer["snapshot_id"], records.c.kind.in_(_SNAPSHOTS))).mappings().first()
            if row is None or row["payload"].get("status") != "ready":
                raise Conflict("Active knowledge snapshot is not ready")
            return self._record(row)

    def get_knowledge_pointer(self):
        with self._transaction() as connection:
            return dict(self._pointer(connection))

    def activate_snapshot(self, snapshot_id: str, expected_current=_UNSET, *, expected_pointer_version=None):
        """Internal knowledge-admin operation: callers must authorize it."""
        with self._transaction() as connection:
            pointer = self._pointer(connection, lock=True)
            row = connection.execute(select(records).where(
                records.c.id == snapshot_id, records.c.kind.in_(_SNAPSHOTS))).mappings().first()
            if row is None:
                raise NotFound("Knowledge snapshot not found")
            if row["payload"].get("status") != "ready":
                raise Conflict("Only a ready knowledge snapshot can become active")
            if expected_current is not _UNSET and pointer["snapshot_id"] != expected_current:
                raise Conflict("Active knowledge changed", code="snapshot_version_conflict")
            if expected_pointer_version is not None and pointer["state_version"] != expected_pointer_version:
                raise Conflict("Active knowledge changed", code="snapshot_version_conflict")
            if pointer["snapshot_id"] == snapshot_id:
                return self._record(row)
            connection.execute(update(knowledge_pointer).where(
                knowledge_pointer.c.name == "active").values(
                    snapshot_id=snapshot_id, state_version=pointer["state_version"] + 1))
            for asset in connection.execute(select(records).where(
                    records.c.kind.in_(_ASSETS)).with_for_update()).mappings().all():
                payload = _json(asset["payload"])
                if (payload.get("publication_status") == "published" and
                        payload.get("knowledge_snapshot_id") != snapshot_id):
                    payload["publication_status"] = "stale"
                    connection.execute(update(records).where(records.c.id == asset["id"]).values(
                        payload=payload, state_version=asset["state_version"] + 1, updated_at=_now()))
            return self._record(row)

    @staticmethod
    def _publishable(payload, snapshot_id):
        content, approval, facts = payload.get("content"), payload.get("approval") or {}, payload.get("fact_check_report") or {}
        if not isinstance(content, str) or not content.strip() or not isinstance(approval, dict) or not isinstance(facts, dict):
            return False
        checksum, version = content_hash(content), payload.get("version_id")
        return bool(payload.get("content_type") == "faq" and version and snapshot_id and
                    payload.get("knowledge_snapshot_id") == snapshot_id and
                    payload.get("content_sha256") == checksum and
                    payload.get("text_review_status") == "approved" and
                    approval.get("version_id") == version and approval.get("content_sha256") == checksum and
                    approval.get("knowledge_snapshot_id") == snapshot_id and facts.get("passed") is True and
                    facts.get("content_sha256") == checksum and
                    facts.get("knowledge_snapshot_id") == snapshot_id and not facts.get("issues") and
                    facts.get("status") in (None, "pass", "passed"))

    def _asset(self, connection, asset_id, owner_id):
        result = self._bound(connection).get(asset_id, owner_id)
        if result["kind"] not in _ASSETS:
            raise NotFound("FAQ asset not found")
        return result

    @staticmethod
    def _task_matches(connection, payload, owner_id, *, lock=False):
        task_id = payload.get("task_id")
        if not task_id:
            return False
        statement = select(records).where(records.c.id == task_id, records.c.owner_id == owner_id,
                                           records.c.kind.in_(("task", "content_task")))
        if lock:
            statement = statement.with_for_update()
        task = connection.execute(statement).mappings().first()
        if task is None:
            return False
        task_payload = task["payload"]
        current = task_payload.get("current_version_id", task_payload.get("version_id"))
        return ((current is None or current == payload.get("version_id")) and
                (not task_payload.get("content_sha256") or
                 task_payload["content_sha256"] == payload.get("content_sha256")))

    @staticmethod
    def _wiki_publishable(payload, snapshot_id):
        from .support_wiki import legacy_wiki_content, review_digest

        if (payload.get('content_type') != 'wiki' or not snapshot_id or payload.get('knowledge_snapshot_id') != snapshot_id
                or payload.get('text_review_status') != 'approved' or not payload.get('evidence_refs')):
            return False
        try:
            page, content = payload['wiki'], payload['content']
            answer = page['answer']
            return (isinstance(answer, str) and bool(answer.strip()) and
                    payload['wiki_sha256'] == review_digest(payload) and
                    (content == answer or content == legacy_wiki_content(page)) and
                    payload['content_sha256'] == content_hash(content))
        except (KeyError, TypeError):
            return False

    def valid_wiki(self, owner_id: str):
        active = self.get_active_snapshot()
        if active is None:
            return []
        return [asset for asset in self.list('asset', owner_id)
                if asset['payload'].get('publication_status') == 'published'
                and self._wiki_publishable(asset['payload'], active['id'])]

    def _publish_wiki(self, connection, asset, owner_id, snapshot_id):
        tx, payload = self._bound(connection), asset['payload']
        if not self._wiki_publishable(payload, snapshot_id):
            raise Conflict('Wiki 须通过审核，并与当前知识快照一致')
        if payload['publication_status'] == 'published':
            return asset
        if not self._task_matches(connection, payload, owner_id, lock=True):
            raise Conflict('请发布任务中最新审核通过的 Wiki 版本')
        replaced = []
        keys = set(payload['wiki']['question_keys'])
        for page in tx.valid_wiki(owner_id):
            other = page['payload']
            if other['task_id'] == payload['task_id']:
                replaced.append(page)
            elif keys.intersection(other['wiki']['question_keys']):
                raise Conflict('该问题或别名已由另一篇 Wiki 覆盖，请修订原条目')
        for page in replaced:
            tx.update_cas(page['id'], owner_id, page['state_version'],
                          {**page['payload'], 'publication_status':'superseded', 'superseded_by':asset['id']})
        return tx.update_cas(asset['id'], owner_id, asset['state_version'],
                             {**payload, 'publication_status':'published', 'published_at':_now().isoformat()})

    def publish(self, asset_id: str, owner_id: str, expected_version: int):
        with self._transaction() as connection:
            tx, pointer = self._bound(connection), self._pointer(connection, lock=True)
            asset = self._asset(connection, asset_id, owner_id)
            payload = asset["payload"]
            if asset["state_version"] != expected_version:
                raise Conflict("Asset changed; reload before publishing")
            if payload.get("publication_status", "draft") not in ("draft", "published"):
                raise Conflict("This asset version was revoked or became stale")
            if payload.get('content_type') == 'wiki':
                return self._publish_wiki(connection, asset, owner_id, pointer["snapshot_id"])
            if not self._publishable(payload, pointer["snapshot_id"]):
                raise Conflict("Publishing requires current knowledge, approved content and passed facts")
            if not self._task_matches(connection, payload, owner_id, lock=True):
                raise Conflict("Source task is missing or has different current content")
            if payload.get("publication_status") == "published":
                return asset
            payload["publication_status"] = "published"
            payload["published_at"] = _now().isoformat()
            return tx.update_cas(asset_id, owner_id, expected_version, payload)

    def revoke(self, asset_id: str, owner_id: str, expected_version: int):
        with self._transaction() as connection:
            tx = self._bound(connection)
            asset = self._asset(connection, asset_id, owner_id)
            if asset["state_version"] != expected_version:
                raise Conflict("Asset changed; reload before revoking")
            if asset["payload"].get("publication_status") == "revoked":
                return asset
            payload = asset["payload"]
            payload["publication_status"] = "revoked"
            payload["revoked_at"] = _now().isoformat()
            return tx.update_cas(asset_id, owner_id, expected_version, payload)

    def valid_faq(self, owner_id: str, sku: str | None = None, *, product_scope: list[str] | None = None):
        _identity(owner_id, "owner_id")
        scope = set(product_scope or [])
        if sku is not None:
            scope.add(sku)
        with self._transaction() as connection:
            active = self._bound(connection).get_active_snapshot()
            if active is None:
                return []
            result = []
            for row in connection.execute(select(records).where(
                    records.c.owner_id == owner_id, records.c.kind.in_(_ASSETS))).mappings():
                payload = row["payload"]
                if payload.get("publication_status") != "published" or not self._publishable(payload, active["id"]):
                    continue
                if scope and not scope.intersection(payload.get("product_scope") or []):
                    continue
                if not self._task_matches(connection, payload, owner_id):
                    continue
                result.append(self._record(row))
            return result
