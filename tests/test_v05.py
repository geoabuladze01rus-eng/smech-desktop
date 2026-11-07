"""Regression tests for source-page review, material specifications and approvals."""
import hashlib
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app
from pdfio import parse_pdf, render_pdf_source, PdfImportError
from specifications import reconcile_specifications, validate_specifications
import workspace_api
from workspace_store import Store, ReviewDecision

BASE = Path(__file__).resolve().parents[1]
PASSWORD = 'Very-Secure-Password!123'


def specs():
    return [
        {'id': 'x1','name': 'Клей плиточный','unit':'кг','qty':'15.000','code':'K1','source':'Ведомость А'},
        {'id': 'x2','name': 'Клей плиточный','unit':'кг','qty':'5.000','code':'K1','source':'Ведомость Б'},
        {'id': 'x3','name': 'Клей плиточный','unit':'шт','qty':'1','code':'K1','source':'Другая единица'},
    ]


def test_specification_exact_match_aggregation_and_unit_separation():
    check = reconcile_specifications(specs(), [
        {'name':'клей  плиточный', 'unit':'кг','quantity':25},
        {'name':'Доска', 'unit':'м','quantity':2},
    ])
    m={(r['name'],r['unit']):r for r in check['rows']}
    assert m[('клей  плиточный','кг')]['difference']=='-5.000'
    assert m[('клей  плиточный','кг')]['status']=='shortage'
    assert m[('Клей плиточный','шт')]['status']=='unmatched_spec'
    assert m[('Доска','м')]['status']=='unmatched_demand'
    assert check['stats']=={'matched':0,'shortage':1,'surplus':0,'unmatched_spec':1,'unmatched_demand':1}


@pytest.mark.parametrize('bad', [
    [{'id':'x','name':'Труба','unit':'м','qty':'NaN'}],
    [{'id':'x','name':'Труба','unit':'м','qty':'-1'}],
    [{'id':'x','name':'Труба','unit':'м','qty':'0'}],
    [{'id':'x','name':'Труба','unit':'м','qty':'1'}, {'id':'x','name':'Другое','unit':'м','qty':'2'}],
    [{'id':'x','name':'Труба','unit':'','qty':'1'}],
])
def test_specification_validation_rejects_bad_data(bad):
    with pytest.raises(ValueError):validate_specifications(bad)


def test_pdf_row_coordinates_and_api_image_validation():
    raw=(BASE/'Пример ВОР для PDF 0.4.pdf').read_bytes()
    parsed=parse_pdf(raw,filename='Пример.pdf')
    sheet=parsed['sheets'][0]
    origin=sheet['origins'][1]
    assert origin['rowBbox'] != origin['bbox']
    assert origin['rowBbox'][1] < origin['rowBbox'][3]
    img=render_pdf_source(raw,page=origin['page'],row_bbox=origin['rowBbox'])
    assert img.startswith(b'\x89PNG\r\n')
    client=TestClient(app)
    response=client.post(f'/api/pdf/source-page?page={origin["page"]}&bbox='+','.join(map(str, origin['rowBbox'])),
                         files={'file': ('Пример.pdf',io.BytesIO(raw),'application/pdf')})
    assert response.status_code==200, response.text
    assert response.headers['x-smech-sha256']==hashlib.sha256(raw).hexdigest()
    assert response.content.startswith(b'\x89PNG')
    denied=client.post('/api/pdf/source-page?page=1&bbox=-10,0,20,20',
                       files={'file': ('Пример.pdf',io.BytesIO(raw),'application/pdf')})
    assert denied.status_code==400
    denied=client.post('/api/pdf/source-page?page=999',
                       files={'file': ('Пример.pdf',io.BytesIO(raw),'application/pdf')})
    assert denied.status_code in (400,422)


def test_specification_api_does_not_change_cost():
    p={'lines':[{'id':'a','name':'Монтаж','unit':'шт','qty':'10','laborRate':'100','laborSource':'Договор бригады','source':'ВОР','materialRequired':False,'materials':[]}],
       'specifications':[{'id':'s1','name':'Труба','unit':'м','qty':'45','code':'T1','source':'Исходная спецификация'}],
       'extras':{'machinery':0,'delivery':0,'other':0,'overheadPct':0,'markupPct':0,'vatMode':'none','vatPct':0}}
    result=TestClient(app).post('/api/calculate',json={'project':p,'catalog':[]})
    assert result.status_code==200, result.text
    r=result.json()
    assert r['summary']['labor']==1000
    assert r['summary']['materials']==0
    assert r['specificationCheck']['stats']['unmatched_spec']==1


@pytest.fixture
def setup_ws(tmp_path,monkeypatch):
    st=Store('sqlite:///'+str(tmp_path/'data.db'));st.init()
    monkeypatch.setattr(workspace_api,'store',st)
    monkeypatch.setattr(workspace_api,'ENABLED',True)
    workspace_api.FAILS.clear()
    owner=TestClient(app);admin=owner.post('/api/workspace/setup',json={'username':'owner','password':PASSWORD});assert admin.status_code==200,admin.text
    owner_csrf=admin.json()['csrf']
    response=owner.post('/api/workspace/users',json={'username':'engineer','password':PASSWORD},headers={'X-CSRF-Token':owner_csrf});assert response.status_code==200,response.text
    eng=TestClient(app);login=eng.post('/api/workspace/login',json={'username':'engineer','password':PASSWORD});assert login.status_code==200
    eng_csrf=login.json()['csrf']
    state=owner.get('/api/workspace/state').json();project=state['projects'][0];pid=project['id']
    share=owner.post(f'/api/workspace/projects/{pid}/access',json={'username':'engineer','role':'editor'},headers={'X-CSRF-Token':owner_csrf});assert share.status_code==200
    return st,owner,owner_csrf,eng,eng_csrf,project


def _post(cli, path,csrf,json=None):
    return cli.post(path,headers={'X-CSRF-Token':csrf},json=json if json is not None else {})


def _valid(p):
    p['lines']=[{'id':'line1','name':'Монтаж','unit':'шт','qty':'10','source':'Проект 01','laborSource':'Ставка подрядчика','laborRate':'250','materialRequired':False,'materials':[]}]
    return p


def test_review_permissions_two_stages_immutable_and_superseded(setup_ws):
    st, owner,owner_csrf,engineer,eng_csrf,p=setup_ws;pid=p['id']
    p=_valid(p)
    saved=_post(owner,'/api/workspace/sync',owner_csrf,{'upserts':[{'project':p,'expectedRevision':1}]})
    assert saved.status_code==200,saved.text
    shot=_post(owner,f'/api/workspace/projects/{pid}/snapshot',owner_csrf).json()
    sid=shot['id'];hash_=shot['sha256']
    route=f'/api/workspace/projects/{pid}/snapshots/{sid}/review'
    body={'stage':'management','decision':'approved','note':'','expectedProjectRevision':2,'expectedSnapshotHash':hash_}
    assert _post(owner,route,owner_csrf,body).status_code==409  # engineer first
    assert _post(engineer,route,eng_csrf,{**body,'stage':'management'}).status_code==403
    assert _post(owner,route,owner_csrf,{**body,'stage':'engineering'}).status_code==403
    assert _post(engineer,route,eng_csrf,{**body,'stage':'engineering','expectedSnapshotHash':'0'*64}).status_code==409
    approved=_post(engineer,route,eng_csrf,{**body,'stage':'engineering'})
    assert approved.status_code==200,approved.text
    assert approved.json()['state']=='awaiting_manager'
    assert _post(engineer,route,eng_csrf,{**body,'stage':'engineering'}).status_code==409
    accepted=_post(owner,route,owner_csrf,body)
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['state']=='approved'
    with st.Session() as db:
        assert len(db.scalars(select(ReviewDecision)).all())==2
    # Later revision cannot silently inherit approval.
    new=owner.get('/api/workspace/state').json()['projects'][0]
    new['name']='Новая редакция'
    assert _post(owner,'/api/workspace/sync',owner_csrf,{'upserts':[{'project':new,'expectedRevision':2}]}).status_code==200
    view=owner.get(f'/api/workspace/projects/{pid}/reviews').json()
    assert view['items'][0]['state']=='superseded'
    assert _post(owner,route,owner_csrf,body).status_code==409
    # Other project cannot read snapshot / review.
    other=next(x for x in engineer.get('/api/workspace/state').json()['projects'] if x['id']!=pid)
    assert engineer.get(f'/api/workspace/projects/{other["id"]}/reviews').status_code==200
    assert _post(engineer,f'/api/workspace/projects/{other["id"]}/snapshots/{sid}/review',eng_csrf,{**body,'stage':'engineering'}).status_code==404


def test_review_rejection_and_blocking(setup_ws):
    st, owner,owner_csrf,engineer,eng_csrf,p=setup_ws;pid=p['id']
    # New empty object -> snapshot with blockers cannot be approved.
    snap=_post(owner,f'/api/workspace/projects/{pid}/snapshot',owner_csrf).json()
    body={'stage':'engineering','decision':'approved','expectedProjectRevision':1,'expectedSnapshotHash':snap['sha256']}
    route=f'/api/workspace/projects/{pid}/snapshots/{snap["id"]}/review'
    assert _post(engineer,route,eng_csrf,body).status_code==422
    assert _post(engineer,route,eng_csrf,{**body,'decision':'rejected','note':''}).status_code==422
    rej=_post(engineer,route,eng_csrf,{**body,'decision':'rejected','note':'Не указаны объёмы работ'})
    assert rej.status_code==200,rej.text
    assert rej.json()['state']=='rejected'
    assert _post(owner,route,owner_csrf,{**body,'stage':'management','decision':'approved'}).status_code in (409,422)


def test_engineer_cannot_approve_own_snapshot(setup_ws):
    st, owner,owner_csrf,engineer,eng_csrf,p=setup_ws;pid=p['id']
    p=_valid(p)
    assert _post(owner,'/api/workspace/sync',owner_csrf,{'upserts':[{'project':p,'expectedRevision':1}]}).status_code==200
    snap=_post(engineer,f'/api/workspace/projects/{pid}/snapshot',eng_csrf).json()
    reply=_post(engineer,f'/api/workspace/projects/{pid}/snapshots/{snap["id"]}/review',eng_csrf,
                {'stage':'engineering','decision':'approved','expectedProjectRevision':2,'expectedSnapshotHash':snap['sha256']})
    assert reply.status_code==403


def test_specification_discrepancy_requires_written_review_exception(setup_ws):
    st, owner,owner_csrf,engineer,eng_csrf,p=setup_ws
    pid=p['id'];p=_valid(p)
    p['specifications']=[{'id':'spec01','name':'Труба 57 мм','unit':'м','qty':'15','source':'Ведомость материалов'}]
    assert _post(owner,'/api/workspace/sync',owner_csrf,{'upserts':[{'project':p,'expectedRevision':1}]}).status_code==200
    snapshot=_post(owner,f'/api/workspace/projects/{pid}/snapshot',owner_csrf).json()
    route=f'/api/workspace/projects/{pid}/snapshots/{snapshot["id"]}/review'
    payload={'stage':'engineering','decision':'approved','expectedProjectRevision':2,'expectedSnapshotHash':snapshot['sha256'],'note':''}
    denied=_post(engineer,route,eng_csrf,payload)
    assert denied.status_code==422
    assert 'расхождений' in denied.json()['detail']
    approved=_post(engineer,route,eng_csrf,{**payload,'note':'Спецификация является отдельным неполным разделом'})
    assert approved.status_code==200,approved.text
    denied=_post(owner,route,owner_csrf,{**payload,'stage':'management'})
    assert denied.status_code==422
    accepted=_post(owner,route,owner_csrf,{**payload,'stage':'management','note':'Расхождения приняты, оформлена отдельная проверка'})
    assert accepted.status_code==200,accepted.text
