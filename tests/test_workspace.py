"""Workspace tests run entirely against a disposable SQLite DB (no cloud credentials)."""
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace_api
from workspace_store import Store, WorkProject, AuditEntry, ProjectRevision, ImmutableSnapshot
from app import app
from sqlalchemy import select


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    st = Store('sqlite:///' + str(tmp_path / 'workspace.sqlite'))
    st.init()
    monkeypatch.setattr(workspace_api, 'store', st)
    monkeypatch.setattr(workspace_api, 'ENABLED', True)
    workspace_api.FAILS.clear()
    owner = TestClient(app)
    return st, owner


def setup_admin(owner):
    r=owner.post('/api/workspace/setup',json={'username':'boss','password':'Very-Secure-Password!123'})
    assert r.status_code == 200, r.text
    return r.json()['csrf']


def post(client, route, csrf, **kw):
    return client.post(route, headers={'X-CSRF-Token':csrf}, **kw)


def project_of(client):
    r=client.get('/api/workspace/state')
    assert r.status_code==200, r.text
    return r.json()['projects'][0],r.json()


def test_setup_cannot_be_repeated_and_requires_auth(workspace):
    st, owner = workspace
    assert owner.get('/api/workspace/info').json()['enabled']
    assert owner.get('/api/workspace/state').status_code == 401
    csrf = setup_admin(owner)
    assert owner.post('/api/workspace/setup',json={'username':'another','password':'Very-Secure-Password!123'}).status_code==409
    assert owner.post('/api/workspace/users',json={'username':'staff','password':'Very-Secure-Password!123'}).status_code==403
    assert post(owner,'/api/workspace/users',csrf,json={'username':'staff','password':'Another-Secure-Pass!789'}).status_code==200
    r=owner.post('/api/workspace/logout',headers={'X-CSRF-Token':csrf})
    assert r.status_code==200
    assert owner.get('/api/workspace/state').status_code==401
    login=owner.post('/api/workspace/login',json={'username':'boss','password':'Very-Secure-Password!123'})
    assert login.status_code==200
    assert login.json()['csrf']


def test_price_snapshots_roles_and_sharing(workspace):
    st, owner=workspace
    admin_csrf=setup_admin(owner)
    # Admin explicitly creates a new account. No public self-signup is available.
    assert post(owner,'/api/workspace/users',admin_csrf,json={'username':'worker','password':'Another-Secure-Pass!789'}).status_code==200
    worker=TestClient(app)
    auth=worker.post('/api/workspace/login',json={'username':'worker','password':'Another-Secure-Pass!789'})
    assert auth.status_code==200
    worker_csrf=auth.json()['csrf']
    proj, owner_state=project_of(owner)
    owner_id=proj['id']
    other_proj,_=project_of(worker)
    assert owner_id!=other_proj['id']
    assert worker.get(f'/api/workspace/projects/{owner_id}/history').status_code==404
    new=post(worker,f'/api/workspace/projects/{owner_id}/snapshot',worker_csrf)
    assert new.status_code==403
    # Owner grants viewer rights. Viewer can inspect but cannot edit.
    assert post(owner,f'/api/workspace/projects/{owner_id}/access',admin_csrf,json={'username':'worker','role':'viewer'}).status_code==200
    assert owner_id in worker.get('/api/workspace/state').json()['roles']
    proj['name']='Не должен измениться'
    forbidden=post(worker,'/api/workspace/sync',worker_csrf,json={'upserts':[{'project':proj,'expectedRevision':1}]})
    assert forbidden.status_code==403
    assert owner.get('/api/workspace/state').json()['projects'][0]['name']=='Новый объект'
    # Upgrade to editor.
    assert post(owner,f'/api/workspace/projects/{owner_id}/access',admin_csrf,json={'username':'worker','role':'editor'}).status_code==200
    changed=post(worker,'/api/workspace/sync',worker_csrf,json={'upserts':[{'project':proj,'expectedRevision':1}]})
    assert changed.status_code==200, changed.text
    fresh=owner.get('/api/workspace/state').json()
    assert fresh['revisions'][owner_id]==2
    assert any(x['name']=='Не должен измениться' for x in fresh['projects'])
    # Owner who has a stale revision cannot overwrite the newer edit.
    proj['name']='Устаревшая копия'
    conflict=post(owner,'/api/workspace/sync',admin_csrf,json={'upserts':[{'project':proj,'expectedRevision':1}]})
    assert conflict.status_code==409
    # Snapshot is server-generated and immutable even if later prices change.
    saved=post(owner,f'/api/workspace/projects/{owner_id}/snapshot',admin_csrf)
    assert saved.status_code==200, saved.text
    sid=saved.json()['id']
    first=owner.get(f'/api/workspace/projects/{owner_id}/snapshots/{sid}').json()
    now=next(x for x in fresh['projects'] if x['id']==owner_id)
    now['name']='Ещё одна редакция'
    done=post(owner,'/api/workspace/sync',admin_csrf,json={'upserts':[{'project':now,'expectedRevision':2}]})
    assert done.status_code==200, done.text
    assert owner.get(f'/api/workspace/projects/{owner_id}/snapshots/{sid}').json()==first
    assert owner.get(f'/api/workspace/projects/{owner_id}/diff?before=1&after=3').json()['diff']['projectFields']==['name']
    history=owner.get(f'/api/workspace/projects/{owner_id}/history').json()
    assert any(x['action']=='snapshot' for x in history)
    assert any(x['action']=='access' for x in history)


def test_invalid_objects_and_atomic_sync(workspace):
    st, client=workspace
    csrf=setup_admin(client)
    proj, state=project_of(client)
    v=state['revisions'][proj['id']]
    changed=dict(proj,name='Корректный объект')
    malformed=dict(proj,id='new-project',lines=[{'id':'repeat','materials':[]},{'id':'repeat','materials':[]}])
    r=post(client,'/api/workspace/sync',csrf,json={'upserts':[{'project':changed,'expectedRevision':v}, {'project':malformed,'expectedRevision':None}]})
    assert r.status_code==422
    new=client.get('/api/workspace/state').json()
    assert new['revisions'][proj['id']]==v
    assert new['projects'][0]['name']==proj['name']
    # Rejection of a project ID in both upsert and delete.
    r=post(client,'/api/workspace/sync',csrf,json={'upserts':[{'project':changed,'expectedRevision':v}], 'deletes':[{'id':proj['id'],'expectedRevision':v}]})
    assert r.status_code==422


def test_local_import_no_overwrite_and_role_separation(workspace):
    st, owner=workspace
    csrf=setup_admin(owner)
    proj,_=project_of(owner)
    local={'id':'old-local-project','name':'Импортировано из 0.2','lines':[],'extras':{},'version':1}
    catalog=[{'id':'custom-mat','name':'Кирпич','unit':'шт','price':'12'}]
    result=post(owner,'/api/workspace/import-local',csrf,json={'projects':[local],'catalog':catalog})
    assert result.status_code==200, result.text
    after=owner.get('/api/workspace/state').json()
    assert len(after['projects'])==2
    assert after['catalog'] == catalog
    repeated=post(owner,'/api/workspace/import-local',csrf,json={'projects':[local],'catalog':catalog})
    assert repeated.status_code==409
    assert len(owner.get('/api/workspace/state').json()['projects'])==2


def test_owner_only_delete_and_soft_deletion_preserves_history(workspace):
    st, owner=workspace
    csrf=setup_admin(owner)
    proj, state=project_of(owner)
    assert post(owner, '/api/workspace/sync',csrf,json={'deletes':[{'id':proj['id'],'expectedRevision':1}]}).status_code==200
    assert owner.get('/api/workspace/state').json()['projects']==[]
    with st.Session() as db:
        row=db.get(WorkProject,proj['id'])
        assert row.deleted is True
        assert db.scalars(select(ProjectRevision).where(ProjectRevision.project_id==proj['id'])).all()
        assert db.scalars(select(AuditEntry).where(AuditEntry.project_id==proj['id'])).all()


def test_soft_delete_restore_from_trash(workspace):
    st, client=workspace
    csrf=setup_admin(client)
    p,_=project_of(client)
    assert post(client,'/api/workspace/sync',csrf,json={'deletes':[{'id':p['id'],'expectedRevision':1}]}).status_code==200
    trash=client.get('/api/workspace/trash').json()
    assert len(trash)==1
    assert trash[0]['revision']==2
    restore=post(client,f"/api/workspace/projects/{p['id']}/restore",csrf,json={'expectedRevision':2})
    assert restore.status_code==200, restore.text
    new=client.get('/api/workspace/state').json()
    assert new['projects'][0]['name']==p['name']
    assert new['revisions'][p['id']]==3
    assert client.get('/api/workspace/trash').json()==[]
    assert post(client,f"/api/workspace/projects/{p['id']}/restore",csrf,json={'expectedRevision':2}).status_code==409


def test_material_catalog_conflict(workspace):
    st, client=workspace
    csrf=setup_admin(client)
    state=client.get('/api/workspace/state').json()
    one=post(client,'/api/workspace/sync',csrf,json={'catalog':{'items':[{'id':'brick','name':'Кирпич','unit':'шт','price':'10'}], 'expectedRevision':state['catalogRevision']}})
    assert one.status_code==200
    stale=post(client,'/api/workspace/sync',csrf,json={'catalog':{'items':[{'id':'wood','name':'Доска','unit':'м³','price':'9000'}], 'expectedRevision':state['catalogRevision']}})
    assert stale.status_code==409
    assert client.get('/api/workspace/state').json()['catalog'][0]['name']=='Кирпич'


def test_price_changes_are_in_auditable_diff():
    from workspace_store import diff_projects
    def make(price,source):
        return {'lines':[{'id':'l1','name':'Кладка','materials':[{'id':'m1','materialName':'Кирпич','price':price,'priceSource':source,'priceDate':'2026-11-06'}]}], 'extras':{}}
    result=diff_projects(make('10','Прайс 1'),make('14','Прайс 2'))
    assert result['priceChangesCount']==1
    assert result['priceChanges'][0]['before']['price']=='10'
    assert result['priceChanges'][0]['after']['source']=='Прайс 2'


def test_import_legacy_freezes_material_price_before_catalog_update(workspace):
    st, client=workspace
    csrf=setup_admin(client)
    project={'id':'legacy-2019','name':'Старая ВОР','version':1,'extras':{},'lines':[
        {'id':'work1','name':'Кладка','unit':'м²','source':'Чертёж 01','qty':'10',
         'laborRate':'200','materialRequired':True,
         'materials':[{'id':'link1','materialId':'brick','norm':'2','waste':'0'}]}]}
    catalog=[{'id':'brick','name':'Кирпич','unit':'шт','price':'25','source':'Счёт на материалы','date':'2026-11-01'}]
    imported=post(client,'/api/workspace/import-local',csrf,json={'projects':[project],'catalog':catalog})
    assert imported.status_code==200,imported.text
    state=client.get('/api/workspace/state').json()
    new=next(p for p in state['projects'] if p['id']=='legacy-2019')
    link=new['lines'][0]['materials'][0]
    assert link['price']=='25'
    assert link['priceSource']=='Счёт на материалы'
    snap=post(client,'/api/workspace/projects/legacy-2019/snapshot',csrf)
    assert snap.status_code==200,snap.text
    assert snap.json()['summary']['materials']==500.0
    assert snap.json()['blockers']==[]
