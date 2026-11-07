"""Optional authenticated workspace database for Смеч.

SQLite is supported for a single-host test installation; PostgreSQL is the
recommended shared mode. Never enable this on a public network without TLS,
verified access policies, external backups and security review.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text,
    create_engine, event, select, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.exc import IntegrityError


class Base(DeclarativeBase):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = 'smech_users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LoginSession(Base):
    __tablename__ = 'smech_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkProject(Base):
    __tablename__ = 'smech_projects'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False, index=True)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProjectMember(Base):
    __tablename__ = 'smech_members'
    project_id: Mapped[str] = mapped_column(ForeignKey('smech_projects.id', ondelete='CASCADE'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id', ondelete='CASCADE'), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class UserCatalog(Base):
    __tablename__ = 'smech_catalogs'
    user_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class ProjectRevision(Base):
    __tablename__ = 'smech_project_revisions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint('project_id', 'revision', name='uq_smech_project_revision'),)


class AuditEntry(Base):
    __tablename__ = 'smech_audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ImmutableSnapshot(Base):
    __tablename__ = 'smech_snapshots'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('smech_projects.id', ondelete='CASCADE'), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    blockers: Mapped[list] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReviewDecision(Base):
    """Append-only decision for exactly one stage of an immutable snapshot."""
    __tablename__ = 'smech_review_decisions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('smech_projects.id', ondelete='CASCADE'), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey('smech_snapshots.id', ondelete='CASCADE'), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(1000), default='', nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint('snapshot_id', 'stage', name='uq_smech_review_stage'),)


class UserPreference(Base):
    __tablename__ = 'smech_preferences'
    user_id: Mapped[str] = mapped_column(ForeignKey('smech_users.id'), primary_key=True)
    active_project_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_HASH = ph.hash('not-a-real-user-password')


def digest(value: Any) -> str:
    packed = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(packed.encode('utf-8')).hexdigest()


def password_ok(password: str) -> bool:
    return isinstance(password, str) and 12 <= len(password) <= 128


def username_ok(username: str) -> bool:
    return isinstance(username, str) and bool(re.fullmatch(r'[a-zA-Z0-9_.-]{3,60}', username))


def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[a-zA-Z0-9_:-]{1,100}', value))


def sanitize_project(project: Any) -> dict:
    """Do not trust client IDs, snapshots, or obviously oversized structures."""
    if not isinstance(project, dict) or not valid_id(project.get('id')):
        raise ValueError('Некорректный идентификатор объекта')
    result = {k: v for k, v in project.items() if k != 'snapshots'}
    if not isinstance(result.get('name'), str) or not result['name'].strip() or len(result['name']) > 400:
        raise ValueError('Название объекта обязательно (до 400 символов)')
    from specifications import validate_specifications
    result['specifications'] = validate_specifications(result.get('specifications', []))
    lines = result.get('lines')
    if not isinstance(lines, list) or len(lines) > 10000:
        raise ValueError('Неверная ведомость объёмов работ')
    if not isinstance(result.get('extras', {}), dict):
        raise ValueError('Повреждены коммерческие параметры')
    line_ids = set()
    for line in lines:
        if not isinstance(line, dict) or not valid_id(line.get('id')) or line['id'] in line_ids:
            raise ValueError('Некорректные или повторяющиеся идентификаторы работ')
        line_ids.add(line['id'])
        source = line.get('sourceRef')
        if source is not None:
            if not isinstance(source, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(source.get('sha256', ''))):
                raise ValueError('Некорректный цифровой отпечаток исходного PDF')
            if not isinstance(source.get('page'), int) or not 1 <= source['page'] <= 40:
                raise ValueError('Некорректный номер страницы исходного PDF')
            coords = source.get('rowBbox')
            if coords is not None and (not isinstance(coords, list) or len(coords)!=4 or
                    any(isinstance(n, bool) or not isinstance(n,(float,int)) or not 0 <= n <= 20000 for n in coords) or
                    not (coords[0] < coords[2] and coords[1] < coords[3])):
                raise ValueError('Некорректные координаты исходной строки PDF')
        links = line.get('materials') or []
        if not isinstance(links, list) or len(links) > 200:
            raise ValueError('Некорректный набор материалов')
    try:
        packed = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('Объект содержит некорректные числа или значения') from exc
    if len(packed.encode('utf-8')) > 8_000_000:
        raise ValueError('Объект превышает ограничение 8 МБ')
    return result



def freeze_legacy_material_prices(project: dict, catalog: list[dict]) -> dict:
    """Convert v0.1/v0.2 material references to self-contained project price snapshots.

    Never overwrite an existing snapshot, even if the catalog has changed.
    Missing catalog entries stay missing (the calculation engine will block them).
    """
    result = json.loads(json.dumps(project, ensure_ascii=False, allow_nan=False))
    by_id = {str(m.get('id')): m for m in catalog}
    for line in result.get('lines', []):
        for link in line.get('materials') or []:
            m = by_id.get(str(link.get('materialId')))
            if not m:
                continue
            link.setdefault('materialName', m.get('name', ''))
            link.setdefault('materialUnit', m.get('unit', ''))
            link.setdefault('price', m.get('price'))
            link.setdefault('priceSource', m.get('source', ''))
            link.setdefault('priceDate', m.get('date', ''))
    return result


def validate_catalog(items: Any):
    if not isinstance(items, list) or len(items) > 5000:
        raise ValueError('Неверный справочник')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not valid_id(item.get('id')) or item['id'] in seen:
            raise ValueError('Некорректный материал или повтор идентификатора')
        seen.add(item['id'])
    try:
        packed = json.dumps(items, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('Справочник содержит некорректные данные') from exc
    if len(packed.encode('utf-8')) > 3_000_000:
        raise ValueError('Справочник превышает ограничение 3 МБ')
    return items


def diff_projects(a: dict | None, b: dict | None) -> dict:
    a, b = a or {}, b or {}
    la = {str(i['id']): i for i in a.get('lines', []) if isinstance(i, dict) and 'id' in i}
    lb = {str(i['id']): i for i in b.get('lines', []) if isinstance(i, dict) and 'id' in i}
    added = sorted(set(lb) - set(la))
    removed = sorted(set(la) - set(lb))
    changed = []
    for lid in sorted(set(la) & set(lb)):
        if la[lid] != lb[lid]:
            fields = sorted(k for k in set(la[lid]) | set(lb[lid]) if la[lid].get(k) != lb[lid].get(k))
            changed.append({'id': lid, 'name': lb[lid].get('name', ''), 'fields': fields})
    changed_extras = sorted(k for k in set(a.get('extras', {})) | set(b.get('extras', {}))
                            if a.get('extras', {}).get(k) != b.get('extras', {}).get(k))
    price_changes = []
    for lid in sorted(set(la) & set(lb)):
        old_links = {str(x.get('id')): x for x in la[lid].get('materials', []) if isinstance(x, dict)}
        new_links = {str(x.get('id')): x for x in lb[lid].get('materials', []) if isinstance(x, dict)}
        for link_id in sorted(set(old_links) & set(new_links)):
            old, new = old_links[link_id], new_links[link_id]
            if any(old.get(field) != new.get(field) for field in ('price', 'priceDate', 'priceSource')):
                price_changes.append({'lineId': lid, 'material': new.get('materialName', ''),
                                      'before': {'price': old.get('price'), 'date': old.get('priceDate'), 'source': old.get('priceSource')},
                                      'after': {'price': new.get('price'), 'date': new.get('priceDate'), 'source': new.get('priceSource')}})
    return {'added': len(added), 'removed': len(removed), 'changed': len(changed),
            'priceChanges': price_changes[:200], 'priceChangesCount': len(price_changes),
            'addedIds': added[:100], 'removedIds': removed[:100], 'changedLines': changed[:100],
            'changedExtras': changed_extras,
            'projectFields': [k for k in ('name', 'address', 'organization', 'date') if a.get(k) != b.get(k)]}


def initial_project():
    return {'id': new_id(), 'name': 'Новый объект', 'address': '', 'organization': '', 'date': utc_now().date().isoformat(),
            'version': 1, 'demo': False, 'lines': [],
            'extras': {'machinery': 0, 'delivery': 0, 'other': 0, 'employmentChargesPct': 0,
                       'overheadPct': 0, 'markupPct': 0, 'vatMode': 'none', 'vatPct': 0}}


class Store:
    def __init__(self, url: str):
        self.url = url
        settings = {'connect_args': {'check_same_thread': False}} if url.startswith('sqlite:') else {'pool_pre_ping': True}
        self.engine = create_engine(url, future=True, **settings)
        if url.startswith('sqlite:'):
            @event.listens_for(self.engine, 'connect')
            def pragma(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.close()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init(self):
        Base.metadata.create_all(self.engine)

    def has_users(self, session):
        return session.scalar(select(User.id).limit(1)) is not None

    @staticmethod
    def session_user(db, raw_cookie: str | None):
        if not raw_cookie or len(raw_cookie) > 256:
            return None
        tokenhash = hashlib.sha256(raw_cookie.encode()).hexdigest()
        found = db.get(LoginSession, tokenhash)
        if not found:
            return None
        if found.expires_at.replace(tzinfo=timezone.utc) < utc_now():
            db.delete(found)
            return None
        u = db.get(User, found.user_id)
        return (u, found) if u and u.enabled else None

    @staticmethod
    def issue(db, user):
        raw = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=hashlib.sha256(raw.encode()).hexdigest(), user_id=user.id,
                            csrf_token=csrf, expires_at=utc_now() + timedelta(hours=12)))
        return raw, csrf

    @staticmethod
    def create_user(db, username: str, password: str, admin: bool = False):
        username = username.strip().lower()
        if not username_ok(username) or not password_ok(password):
            raise ValueError('Логин: 3–60 латинских символов/цифр; пароль: 12–128 символов')
        if db.scalar(select(User.id).where(User.username == username)):
            raise ValueError('Логин уже занят')
        u = User(id=new_id(), username=username, password_hash=ph.hash(password), is_admin=admin)
        db.add(u)
        db.flush()
        db.add(UserCatalog(user_id=u.id, items=[], revision=1))
        db.add(UserPreference(user_id=u.id))
        return u

    @staticmethod
    def login(db, username, password):
        name = str(username).strip().lower()
        user = db.scalar(select(User).where(User.username == name))
        try:
            success = ph.verify(user.password_hash if user else DUMMY_HASH, password)
        except (VerifyMismatchError, VerificationError):
            success = False
        return user if user and user.enabled and success else None

    @staticmethod
    def access(db, project_id, user_id):
        p = db.get(WorkProject, project_id)
        if not p or p.deleted:
            return None, None
        if p.owner_id == user_id:
            return p, 'owner'
        m = db.get(ProjectMember, (project_id, user_id))
        return (p, m.role) if m else (None, None)

    @staticmethod
    def record_revision(db, project, actor_id, action, before=None):
        details = diff_projects(before, project.body)
        db.add(ProjectRevision(id=new_id(), project_id=project.id, revision=project.revision, actor_id=actor_id,
                               body=project.body, sha256=digest(project.body)))
        db.add(AuditEntry(id=new_id(), project_id=project.id, actor_id=actor_id,
                          revision=project.revision, action=action, details=details))

    def create_project(self, db, user_id, body):
        body = sanitize_project(body)
        if db.get(WorkProject, body['id']):
            raise ValueError('Идентификатор объекта уже существует')
        rec = WorkProject(id=body['id'], owner_id=user_id, body=body, revision=1)
        db.add(rec)
        db.flush()
        self.record_revision(db, rec, user_id, 'created')
        return rec

    def state(self, db, user_id):
        owned = db.scalars(select(WorkProject).where(WorkProject.owner_id == user_id, WorkProject.deleted == False)).all()
        memberships = db.scalars(select(ProjectMember).where(ProjectMember.user_id == user_id)).all()
        others = [db.get(WorkProject, m.project_id) for m in memberships]
        recs = sorted({p.id: p for p in (owned + others) if p is not None and not p.deleted}.values(), key=lambda p: p.created_at)
        projects = []
        revs = {}
        roles = {}
        for p in recs:
            _, role = self.access(db, p.id, user_id)
            body = json.loads(json.dumps(p.body, ensure_ascii=False))
            snapshots = db.scalars(select(ImmutableSnapshot).where(ImmutableSnapshot.project_id == p.id)
                                   .order_by(ImmutableSnapshot.at.desc()).limit(12)).all()
            body['snapshots'] = [self.snapshot_view(s, include_body=False) for s in reversed(snapshots)]
            projects.append(body)
            revs[p.id] = p.revision
            roles[p.id] = role
        catalog = db.get(UserCatalog, user_id)
        preference = db.get(UserPreference, user_id)
        active = preference.active_project_id if preference and preference.active_project_id in revs else (projects[0]['id'] if projects else None)
        return {'projects': projects, 'active': active, 'catalog': catalog.items if catalog else [],
                'revisions': revs, 'roles': roles, 'catalogRevision': catalog.revision if catalog else 0}

    @staticmethod
    def snapshot_view(snapshot, include_body=False):
        body = {'id': snapshot.id, 'at': snapshot.at.isoformat(), 'version': snapshot.revision,
                'summary': snapshot.summary, 'blockers': snapshot.blockers,
                'status': 'Предварительный' if snapshot.blockers else 'Формально заполнен',
                'sha256': snapshot.sha256}
        if include_body:
            body['project'] = snapshot.body
        return body
