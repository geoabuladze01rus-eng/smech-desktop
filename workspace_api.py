"""Authenticated multi-user API, opt-in via SMECH_ENABLE_WORKSPACE=1."""
from __future__ import annotations
import hashlib
import os
import secrets
import time
from collections import defaultdict
from datetime import timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from workspace_store import (Store, User, LoginSession, ProjectMember, WorkProject, UserCatalog,
                             UserPreference, ImmutableSnapshot, ProjectRevision, AuditEntry, ReviewDecision,
                             digest, utc_now, new_id, password_ok, sanitize_project, validate_catalog,
                             diff_projects, initial_project, freeze_legacy_material_prices)
from engine import calculate
from specifications import reconcile_specifications

router = APIRouter(prefix='/api/workspace', tags=['Workspace'])
ENABLED = os.getenv('SMECH_ENABLE_WORKSPACE', '0') == '1'
DB_URL = os.getenv('SMECH_DATABASE_URL', '') or 'sqlite:///' + os.path.join(os.path.dirname(__file__), 'smech-workspace.db')
store = Store(DB_URL) if ENABLED else None
if store is not None:
    store.init()

# A deployment must have a separate TLS reverse proxy and regular database backups.
COOKIE = 'smech_workspace_session'
FAILS: dict[str, list[float]] = defaultdict(list)


def enabled():
    if not ENABLED:
        raise HTTPException(404, 'Рабочее пространство отключено')
    return store


def get_user(request: Request, st: Store = Depends(enabled)):
    with st.Session() as db:
        found = st.session_user(db, request.cookies.get(COOKIE))
        if not found:
            raise HTTPException(401, 'Требуется вход в рабочее пространство')
        u, ses = found
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            csrf = request.headers.get('x-csrf-token', '')
            if not secrets.compare_digest(csrf, ses.csrf_token):
                raise HTTPException(403, 'Проверка CSRF не пройдена')
        return {'id': u.id, 'username': u.username, 'is_admin': u.is_admin, 'csrf': ses.csrf_token}


def set_cookie(resp: Response, token: str, request: Request):
    # Deployments behind a TLS terminator must explicitly set SMECH_SECURE_COOKIES=1.
    secure = os.getenv('SMECH_SECURE_COOKIES') == '1' or request.url.scheme == 'https'
    resp.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite='strict',
                    max_age=43200, path='/api/workspace')


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    password: str = Field(min_length=12, max_length=128)
    setupKey: str = ''


class LoginCredentials(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=128)


@router.get('/info')
def info():
    if not ENABLED:
        return {'enabled': False}
    with store.Session() as db:
        return {'enabled': True, 'initialized': store.has_users(db),
                'backend': 'PostgreSQL' if DB_URL.startswith('postgresql') else 'SQLite (для локальной проверки)'}


@router.post('/setup')
def setup(payload: Credentials, request: Request, response: Response, st: Store = Depends(enabled)):
    configured_key = os.getenv('SMECH_SETUP_TOKEN')
    host = request.client.host if request.client else ''
    if not configured_key and host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(403, 'Первичная настройка разрешена только локально либо по ключу SMECH_SETUP_TOKEN')
    if configured_key and not secrets.compare_digest(configured_key, payload.setupKey):
        raise HTTPException(403, 'Неверный ключ первичной настройки')
    if not password_ok(payload.password):
        raise HTTPException(422, 'Пароль должен содержать 12–128 символов')
    with st.Session.begin() as db:
        if st.has_users(db):
            raise HTTPException(409, 'Первичный пользователь уже создан')
        try:
            u = st.create_user(db, payload.username, payload.password, admin=True)
            st.create_project(db, u.id, initial_project())
            cookie, csrf = st.issue(db, u)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    set_cookie(response, cookie, request)
    return {'username': u.username, 'is_admin': True, 'csrf': csrf}


@router.post('/login')
def login(payload: LoginCredentials, request: Request, response: Response, st: Store = Depends(enabled)):
    key = (request.client.host if request.client else '-') + '/' + str(payload.username).lower()
    now = time.monotonic()
    FAILS[key] = [t for t in FAILS[key] if now-t < 900]
    if len(FAILS[key]) >= 6:
        raise HTTPException(429, 'Слишком много неудачных попыток. Повторите через 15 минут.')
    with st.Session.begin() as db:
        user = st.login(db, payload.username, payload.password)
        if user is None:
            FAILS[key].append(now)
            raise HTTPException(401, 'Неверный логин или пароль')
        FAILS.pop(key, None)
        cookie, csrf = st.issue(db, user)
    set_cookie(response, cookie, request)
    return {'username': user.username, 'is_admin': user.is_admin, 'csrf': csrf}


@router.get('/me')
def me(user=Depends(get_user)):
    return user


@router.post('/logout')
def logout(request: Request, response: Response, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session.begin() as db:
        tok = request.cookies.get(COOKIE, '')
        row = db.get(LoginSession, hashlib.sha256(tok.encode()).hexdigest())
        if row:
            db.delete(row)
    response.delete_cookie(COOKIE, path='/api/workspace')
    return {'ok': True}


@router.get('/users')
def users(user=Depends(get_user), st: Store = Depends(enabled)):
    if not user['is_admin']:
        raise HTTPException(403, 'Управление пользователями разрешено администратору')
    with st.Session() as db:
        return [{'username': r.username, 'is_admin': r.is_admin, 'enabled': r.enabled} for r in db.scalars(select(User).order_by(User.username)).all()]


@router.post('/users')
def add_user(payload: Credentials, user=Depends(get_user), st: Store = Depends(enabled)):
    if not user['is_admin']:
        raise HTTPException(403, 'Недостаточно прав')
    with st.Session.begin() as db:
        try:
            created = st.create_user(db, payload.username, payload.password)
            st.create_project(db, created.id, initial_project())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {'username': created.username, 'enabled': True}


@router.get('/state')
def state(user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        return st.state(db, user['id'])


class ChangedProject(BaseModel):
    project: dict
    expectedRevision: int | None = None


class DeletedProject(BaseModel):
    id: str
    expectedRevision: int


class ChangedCatalog(BaseModel):
    items: list[dict]
    expectedRevision: int


class SyncPayload(BaseModel):
    upserts: list[ChangedProject] = Field(default_factory=list, max_length=100)
    deletes: list[DeletedProject] = Field(default_factory=list, max_length=100)
    catalog: ChangedCatalog | None = None
    active: str | None = None


@router.post('/sync')
def sync(payload: SyncPayload, user=Depends(get_user), st: Store = Depends(enabled)):
    actor = user['id']
    requested_ids = [x.project.get('id') for x in payload.upserts] + [x.id for x in payload.deletes]
    if len(set(requested_ids)) != len(requested_ids):
        raise HTTPException(422, 'Объект указан в нескольких операциях')
    with st.Session.begin() as db:
        try:
            # Validate all updates / permissions BEFORE modifying anything. This avoids partial saves.
            candidates = []
            for change in payload.upserts:
                body = sanitize_project(change.project)
                current, role = st.access(db, body['id'], actor)
                if current is None:
                    if db.get(WorkProject, body['id']) is not None or change.expectedRevision is not None:
                        raise HTTPException(409, 'Конфликт идентификатора нового объекта')
                else:
                    if role not in ('owner', 'editor'):
                        raise HTTPException(403, 'Недостаточно прав на изменение объекта')
                    if current.revision != change.expectedRevision:
                        raise HTTPException(409, f'Объект «{body["name"]}» изменён другим пользователем. Сохранение отменено.')
                candidates.append((body, current))
            deletions = []
            for change in payload.deletes:
                current, role = st.access(db, change.id, actor)
                if role != 'owner':
                    raise HTTPException(403, 'Удалить объект может только владелец')
                if current.revision != change.expectedRevision:
                    raise HTTPException(409, 'Конфликт версии при удалении объекта')
                deletions.append(current)
            catalog = None
            if payload.catalog is not None:
                validate_catalog(payload.catalog.items)
                catalog = db.get(UserCatalog, actor)
                if catalog is None or catalog.revision != payload.catalog.expectedRevision:
                    raise HTTPException(409, 'Справочник изменился. Загрузите свежую версию.')
            # Perform optimistic UPDATE operations, checking that revisions still match.
            for body, old in candidates:
                if old is None:
                    st.create_project(db, actor, body)
                else:
                    old_body = old.body
                    result = db.execute(update(WorkProject).where(WorkProject.id == old.id,
                            WorkProject.revision == old.revision, WorkProject.deleted == False)
                            .values(body=body, revision=old.revision+1, updated_at=utc_now()).execution_options(synchronize_session=False))
                    if result.rowcount != 1:
                        raise HTTPException(409, 'Параллельное изменение объекта: данные не сохранены')
                    st.record_revision(db, WorkProject(id=old.id, owner_id=old.owner_id, body=body, revision=old.revision+1), actor, 'updated', before=old_body)
            for old in deletions:
                result = db.execute(update(WorkProject).where(WorkProject.id == old.id,
                        WorkProject.revision == old.revision, WorkProject.deleted == False)
                        .values(deleted=True, revision=old.revision+1, updated_at=utc_now()).execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    raise HTTPException(409, 'Параллельное удаление объекта: действие отменено')
                db.add(AuditEntry(id=new_id(), project_id=old.id, actor_id=actor,
                                  revision=old.revision+1, action='deleted', details={'name': old.body.get('name')}))
            if payload.catalog is not None:
                result = db.execute(update(UserCatalog).where(UserCatalog.user_id == actor,
                        UserCatalog.revision == catalog.revision)
                        .values(items=payload.catalog.items, revision=catalog.revision+1).execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    raise HTTPException(409, 'Конфликт версий справочника')
            if payload.active is not None:
                pref = db.get(UserPreference, actor)
                if pref:
                    # Do not allow selecting an inaccessible project as active.
                    target, role = st.access(db, payload.active, actor)
                    if not target and all(body['id'] != payload.active for body, _ in candidates):
                        raise HTTPException(403, 'Нет доступа к выбранному объекту')
                    pref.active_project_id = payload.active
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    with st.Session() as db:
        return st.state(db, actor)


class SharePayload(BaseModel):
    username: str
    role: str


@router.get('/projects/{project_id}/access')
def list_access(project_id: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        result = [{'username': db.get(User, p.owner_id).username, 'role': 'owner'}]
        for m in db.scalars(select(ProjectMember).where(ProjectMember.project_id == p.id)):
            result.append({'username': db.get(User, m.user_id).username, 'role': m.role})
        return result


@router.post('/projects/{project_id}/access')
def share(project_id: str, payload: SharePayload, user=Depends(get_user), st: Store = Depends(enabled)):
    if payload.role not in ('viewer', 'editor'):
        raise HTTPException(422, 'Допускаются роли viewer или editor')
    with st.Session.begin() as db:
        p, role = st.access(db, project_id, user['id'])
        if role != 'owner':
            raise HTTPException(403, 'Доступом управляет владелец объекта')
        other = db.scalar(select(User).where(User.username == payload.username.lower()))
        if not other or not other.enabled:
            raise HTTPException(404, 'Пользователь не найден')
        if other.id == p.owner_id:
            raise HTTPException(422, 'Владелец уже имеет полный доступ')
        member = db.get(ProjectMember, (project_id, other.id))
        if member:
            member.role = payload.role
        else:
            db.add(ProjectMember(project_id=project_id, user_id=other.id, role=payload.role))
        db.add(AuditEntry(id=new_id(), project_id=p.id, actor_id=user['id'], revision=p.revision,
                          action='access', details={'username': other.username, 'role': payload.role}))
    return {'ok': True}


@router.delete('/projects/{project_id}/access/{username}')
def revoke(project_id: str, username: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session.begin() as db:
        p, role = st.access(db, project_id, user['id'])
        if role != 'owner':
            raise HTTPException(403, 'Доступом управляет владелец объекта')
        other = db.scalar(select(User).where(User.username == username.lower()))
        if not other:
            raise HTTPException(404, 'Пользователь не найден')
        m = db.get(ProjectMember, (p.id, other.id))
        if not m:
            raise HTTPException(404, 'Доступ не был выдан')
        db.delete(m)
        db.add(AuditEntry(id=new_id(), project_id=p.id, actor_id=user['id'], revision=p.revision,
                          action='access_revoked', details={'username': other.username}))
    return {'ok': True}


@router.get('/projects/{project_id}/history')
def history(project_id: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        entries = db.scalars(select(AuditEntry).where(AuditEntry.project_id == project_id)
                             .order_by(AuditEntry.at.desc()).limit(200)).all()
        return [{'id': a.id, 'at': a.at.isoformat(), 'revision': a.revision, 'action': a.action,
                 'actor': db.get(User, a.actor_id).username, 'details': a.details} for a in entries]


@router.get('/projects/{project_id}/diff')
def diff(project_id: str, before: int, after: int, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        a = db.scalar(select(ProjectRevision).where(ProjectRevision.project_id == project_id, ProjectRevision.revision == before))
        b = db.scalar(select(ProjectRevision).where(ProjectRevision.project_id == project_id, ProjectRevision.revision == after))
        if a is None or b is None:
            raise HTTPException(404, 'Не найдены указанные версии')
        return {'before': a.revision, 'after': b.revision, 'diff': diff_projects(a.body, b.body),
                'sha256Before': a.sha256, 'sha256After': b.sha256}


@router.post('/projects/{project_id}/snapshot')
def create_snapshot(project_id: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session.begin() as db:
        p, role = st.access(db, project_id, user['id'])
        if role not in ('owner', 'editor'):
            raise HTTPException(403, 'Нет прав на создание снимка')
        calculation = calculate(p.body, [])
        if len(p.body.get('lines', [])) > 10000:
            raise HTTPException(413, 'Слишком большая ВОР')
        record = ImmutableSnapshot(id=new_id(), project_id=p.id, actor_id=user['id'],
                                   revision=p.revision, body=p.body, summary=calculation['summary'],
                                   blockers=calculation['blockers'], sha256=digest({'body': p.body, 'summary': calculation['summary']}))
        db.add(record)
        db.add(AuditEntry(id=new_id(), project_id=p.id, actor_id=user['id'], revision=p.revision,
                          action='snapshot', details={'snapshotId': record.id, 'sha256': record.sha256}))
    return st.snapshot_view(record)


@router.get('/projects/{project_id}/snapshots/{snapshot_id}')
def download_snapshot(project_id: str, snapshot_id: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        snap = db.get(ImmutableSnapshot, snapshot_id)
        if not snap or snap.project_id != project_id:
            raise HTTPException(404, 'Снимок не найден')
        return {'format': 'smech-server-snapshot', 'version': 3, 'snapshot': st.snapshot_view(snap, include_body=True)}


class ImportPayload(BaseModel):
    projects: list[dict] = Field(max_length=200)
    catalog: list[dict] = Field(max_length=5000)


@router.post('/import-local')
def import_local(payload: ImportPayload, user=Depends(get_user), st: Store = Depends(enabled)):
    try:
        catalog = validate_catalog(payload.catalog)
        projects = [sanitize_project(freeze_legacy_material_prices(p, catalog)) for p in payload.projects]
        if len({p['id'] for p in projects}) != len(projects):
            raise ValueError('В резервной копии повторяются идентификаторы объектов')
        with st.Session.begin() as db:
            for p in projects:
                if db.get(WorkProject, p['id']):
                    raise HTTPException(409, f'Объект «{p["name"]}» уже существует. Импорт отменён.')
            current_catalog = db.get(UserCatalog, user['id'])
            existing = {item['id'] for item in current_catalog.items}
            if any(m['id'] in existing for m in catalog):
                raise HTTPException(409, 'Обнаружены совпадения материалов. Импорт отменён.')
            for p in projects:
                st.create_project(db, user['id'], p)
            current_catalog.items = list(current_catalog.items) + catalog
            current_catalog.revision += 1
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'imported': len(projects), 'materials': len(catalog)}

@router.get('/trash')
def trash(user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        items = db.scalars(select(WorkProject).where(WorkProject.owner_id == user['id'], WorkProject.deleted == True)).all()
        return [{'id': p.id, 'name': p.body.get('name',''), 'revision': p.revision,
                 'deletedAt': p.updated_at.isoformat()} for p in items]


class RestorePayload(BaseModel):
    expectedRevision: int


@router.post('/projects/{project_id}/restore')
def restore_project(project_id: str, payload: RestorePayload,
                    user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session.begin() as db:
        p = db.get(WorkProject, project_id)
        if not p or p.owner_id != user['id']:
            raise HTTPException(404, 'Удалённый объект не найден')
        if not p.deleted:
            raise HTTPException(409, 'Объект не удалён')
        if p.revision != payload.expectedRevision:
            raise HTTPException(409, 'Редакция изменилась. Обновите список удалённых объектов.')
        result = db.execute(update(WorkProject)
                    .where(WorkProject.id == p.id, WorkProject.revision == p.revision, WorkProject.deleted == True)
                    .values(deleted=False, revision=p.revision+1, updated_at=utc_now())
                    .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(409, 'Конфликт восстановления объекта')
        restored = WorkProject(id=p.id, owner_id=p.owner_id, body=p.body, revision=p.revision+1)
        st.record_revision(db, restored, user['id'], 'restored', before=p.body)
    return {'restored': True, 'id': project_id}

# --- Review workflow v0.5: decisions refer only to immutable server snapshots. ---
class ReviewRequest(BaseModel):
    stage: str
    decision: str
    note: str = Field(default='', max_length=1000)
    expectedProjectRevision: int = Field(ge=1)
    expectedSnapshotHash: str = Field(min_length=64, max_length=64)


def _review_view(db, snap, current_revision):
    decisions = db.scalars(select(ReviewDecision).where(ReviewDecision.snapshot_id == snap.id)
                           .order_by(ReviewDecision.at.asc())).all()
    timeline = [{'stage': d.stage, 'decision': d.decision, 'actor': db.get(User, d.actor_id).username,
                 'note': d.note, 'at': d.at.isoformat(), 'snapshotHash': d.snapshot_hash}
                for d in decisions]
    by_stage = {x['stage']: x for x in timeline}
    if snap.revision != current_revision:
        state = 'superseded'
    elif 'engineering' not in by_stage:
        state = 'awaiting_engineer'
    elif by_stage['engineering']['decision'] == 'rejected':
        state = 'rejected'
    elif 'management' not in by_stage:
        state = 'awaiting_manager'
    else:
        state = 'approved' if by_stage['management']['decision'] == 'approved' else 'rejected'
    return {'snapshotId': snap.id, 'snapshotHash': snap.sha256, 'snapshotRevision': snap.revision,
            'currentRevision': current_revision, 'blockers': snap.blockers, 'state': state,
            'decisions': timeline}


@router.get('/projects/{project_id}/reviews')
def reviews(project_id: str, user=Depends(get_user), st: Store = Depends(enabled)):
    with st.Session() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        snapshots = db.scalars(select(ImmutableSnapshot).where(ImmutableSnapshot.project_id == project_id)
                               .order_by(ImmutableSnapshot.at.desc()).limit(50)).all()
        return {'role': role, 'items': [_review_view(db, s, p.revision) for s in snapshots]}


@router.post('/projects/{project_id}/snapshots/{snapshot_id}/review')
def review_snapshot(project_id: str, snapshot_id: str, payload: ReviewRequest,
                    user=Depends(get_user), st: Store = Depends(enabled)):
    if payload.stage not in ('engineering', 'management') or payload.decision not in ('approved', 'rejected'):
        raise HTTPException(422, 'Недопустимый этап или решение')
    note = payload.note.strip()
    if payload.decision == 'rejected' and len(note) < 5:
        raise HTTPException(422, 'Для отклонения укажите мотивировку от 5 символов')
    with st.Session.begin() as db:
        p, role = st.access(db, project_id, user['id'])
        if not p:
            raise HTTPException(404, 'Объект не найден')
        snap = db.get(ImmutableSnapshot, snapshot_id)
        if not snap or snap.project_id != p.id:
            raise HTTPException(404, 'Архивная версия не найдена')
        if p.revision != payload.expectedProjectRevision or p.revision != snap.revision:
            raise HTTPException(409, 'Объект изменился после создания снимка: требуется новая версия для согласования')
        if not secrets.compare_digest(snap.sha256, payload.expectedSnapshotHash):
            raise HTTPException(409, 'Хеш архивного расчёта не совпадает: согласование отменено')
        if payload.stage == 'engineering':
            if role != 'editor':
                raise HTTPException(403, 'Проверку инженера выполняет назначенный редактор объекта')
            if snap.actor_id == user['id']:
                raise HTTPException(403, 'Автор снимка не может самостоятельно провести инженерную проверку')
        elif role != 'owner':
            raise HTTPException(403, 'Утверждение руководителя доступно только владельцу объекта')
        old = db.scalar(select(ReviewDecision).where(ReviewDecision.snapshot_id == snap.id,
                                                  ReviewDecision.stage == payload.stage))
        if old:
            raise HTTPException(409, 'Решение по этапу уже зафиксировано и не может быть изменено')
        if payload.decision == 'approved' and snap.blockers:
            raise HTTPException(422, 'Расчёт содержит блокирующие замечания и не может быть утверждён')
        if payload.decision == 'approved' and snap.body.get('specifications'):
            # A mismatch can be legitimate (packing, partial scope), but the reviewer
            # must document the exception. Never infer equivalence of units/materials.
            summary_check = calculate(snap.body, [])
            spec_check = reconcile_specifications(snap.body['specifications'], summary_check['resources'])
            unresolved = sum(v for k, v in spec_check['stats'].items() if k != 'matched')
            if unresolved and len(note) < 10:
                raise HTTPException(422, f'В спецификации {unresolved} расхождений. Укажите мотивировку согласования от 10 символов.')
        engineer = db.scalar(select(ReviewDecision).where(ReviewDecision.snapshot_id == snap.id,
                                                       ReviewDecision.stage == 'engineering'))
        if payload.stage == 'management' and (engineer is None or engineer.decision != 'approved'):
            raise HTTPException(409, 'Сначала требуется положительное решение инженера')
        decision = ReviewDecision(id=new_id(), project_id=p.id, snapshot_id=snap.id,
                                  actor_id=user['id'], stage=payload.stage,
                                  decision=payload.decision, note=note, snapshot_hash=snap.sha256)
        db.add(decision)
        db.add(AuditEntry(id=new_id(), project_id=p.id, actor_id=user['id'],
                          revision=p.revision, action='review', details={'snapshotId': snap.id,
                          'stage': payload.stage, 'decision': payload.decision, 'sha256': snap.sha256}))
        try:
            db.flush()
        except IntegrityError as exc:
            raise HTTPException(409, 'Решение по этапу уже принято другим сотрудником') from exc
        result = _review_view(db, snap, p.revision)
    return result
