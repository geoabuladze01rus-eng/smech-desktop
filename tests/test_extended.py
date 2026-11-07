from pathlib import Path
import io
import json
import sys
import zipfile
from decimal import Decimal
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import calculate, number
from excelio import create_xlsx, parse_xlsx
from fastapi.testclient import TestClient
from app import app


def line(**kwargs):
    base = {'id': 'l1', 'name': 'Штукатурка', 'unit': 'м²', 'source': 'Чертёж А-10', 'qty': '10',
            'laborRate': '500', 'materialRequired': True, 'materials': []}
    base.update(kwargs)
    return base


def test_frozen_price_snapshot_survives_catalog_change_and_delete():
    link = {'materialId': 'a', 'materialName': 'Смесь', 'materialUnit': 'кг', 'price': '10',
            'priceSource': 'Счёт №1', 'priceDate': '2026-10-01', 'norm': '2', 'waste': 0}
    proj = {'lines': [line(materials=[link])]}
    first = calculate(proj, [{'id': 'a', 'name': 'Смесь', 'unit': 'кг', 'price': 10}])
    changed = calculate(proj, [{'id': 'a', 'name': 'Другое имя', 'unit': 'т', 'price': 99}])
    deleted = calculate(proj, [])
    assert first['summary']['materials'] == 200
    assert changed['summary']['materials'] == 200
    assert deleted['summary']['materials'] == 200
    assert not first['incomplete']


def test_unreviewed_materials_is_blocker_and_possible_duplicate_is_warning():
    p = {'lines': [line(id='one', materials=[], materialRequired=None),
                   line(id='two', materials=[], materialRequired=None)]}
    # use dict union to avoid duplicate keyword arguments in line helper
    result = calculate(p, [])
    assert result['incomplete']
    assert len(result['positions']) == 2
    assert any('не определено' in b for b in result['blockers'])
    assert any('возможный дубль' in w for w in result['warnings'])


def test_russian_number_guards():
    assert number('1 234,56') == Decimal('1234.56')
    assert number('0,05') == Decimal('0.05')
    for bad in ('1,2,3', '1.2.3', '1e1000', 'NaN', '-1', '12 34', '1 000 0', '1000000000001'):
        with pytest.raises(ValueError):
            number(bad)


def test_payroll_on_labor_and_vat_mode():
    p = {'lines': [line(materialRequired=False)], 'extras': {'employmentChargesPct': '30',
         'vatMode': 'none', 'vatPct': '22', 'markupPct': 20}}
    a = calculate(p, [])
    assert a['summary']['labor'] == 5000
    assert a['summary']['employmentCharges'] == 1500
    assert a['summary']['total'] == 7800
    assert a['summary']['vat'] == 0
    p['extras']['vatMode'] = 'rate'
    b = calculate(p, [])
    assert b['summary']['vat'] == 1716
    assert b['summary']['total'] == 9516


def test_Excel_export_escapes_xml_and_does_not_execute_formula():
    book = create_xlsx([{'name': 'ВОР', 'rows': [['Названия', 'Количество'], ['<дверь & окно>', '=2+3']]}])
    parsed = parse_xlsx(book)
    assert parsed[0]['rows'][1][0] == '<дверь & окно>'
    assert parsed[0]['rows'][1][1] == '=2+3'


def test_xlsx_warns_about_cached_formulas():
    original = create_xlsx([{'name': 'ВОР', 'rows': [['Объём'], [10]]}])
    temp = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(temp, 'w') as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == 'xl/worksheets/sheet1.xml':
                content = content.replace(b'<c r="A2"><v>10</v></c>', b'<c r="A2"><f>2*5</f><v>10</v></c>')
            dst.writestr(item, content)
    data = parse_xlsx(temp.getvalue())
    assert data[0]['rows'][1][0] == '10'
    assert 'формула Excel' in data[0]['warnings'][0]


def test_api_has_security_headers_and_health():
    client = TestClient(app)
    home = client.get('/')
    assert home.status_code == 200
    assert "script-src 'self'" in home.headers['content-security-policy']
    assert client.get('/api/health').json()['version'] == '0.6-desktop'
    assert client.post('/api/calculate', json={'project': {'lines': []}, 'catalog': []}, headers={'Origin': 'https://evil.example'}).status_code == 403
    okay = client.post('/api/calculate', json={'project': {'lines': []}, 'catalog': []})
    assert okay.status_code == 200
    assert okay.json()['incomplete']


def test_api_import_roundtrip():
    client = TestClient(app)
    book = create_xlsx([{'name': 'ВОР', 'rows': [['Наименование', 'Кол-во'], ['Плитка', 10]]}])
    res = client.post('/api/import', files={'file': ('vor.xlsx', book, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert res.status_code == 200
    assert res.json()['sheets'][0]['rows'][1][0] == 'Плитка'
    assert client.post('/api/import', files={'file': ('unsafe.exe', b'foo', 'application/octet-stream')}).status_code == 400


def test_zero_prices_rates_and_norms_are_blockers():
    p = {'lines': [line(laborRate='0', materials=[{'materialId': 'a', 'materialName': 'Краска', 'materialUnit': 'л', 'norm': '0', 'waste': 0, 'price': '0', 'priceSource': 'Тест'}])]}
    r = calculate(p, [])
    assert r['incomplete']
    assert any('положительной сдельной' in x for x in r['blockers'])
    assert any('положительной закупочной' in x for x in r['blockers'])
    assert any('положительная норма' in x for x in r['blockers'])


def test_missing_material_required_is_blocker():
    result = calculate({'lines':[{'id':'x','name':'Работа без флага','unit':'шт','source':'Документ',
                                  'qty':1,'laborRate':500,'materials':[]}]},[])
    assert result['incomplete']
    assert any('не определено' in x for x in result['blockers'])


def test_comma_decimal_guard_on_long_fraction():
    with pytest.raises(ValueError):
        number('0,1234567890123')
