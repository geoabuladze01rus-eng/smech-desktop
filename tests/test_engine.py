import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import calculate, number, needed
from excelio import create_xlsx, parse_xlsx
from decimal import Decimal


def test_number_parse_russian_and_missing():
    assert number('1 234,50') == Decimal('1234.50')
    assert number('', optional=True) is None
    for value in ('-1','NaN','Infinity','abc'):
        try: number(value)
        except ValueError: pass
        else: assert False, f'expected ValueError: {value}'


def test_procurement_rounding():
    assert needed(Decimal('1.001'), 'шт') == Decimal('2')
    assert needed(Decimal('1.2341'), 'кг') == Decimal('1.235')


def test_simple_labor_materials_cost():
    project={'lines':[{'id':'1','name':'Плитка','unit':'м²','qty':'10','source':'Тестовый проект','laborRate':'600','materialRequired':True,
                      'materials':[{'materialId':'m','norm':'1','waste':'10'}]}],
             'extras':{'machinery':'1000','delivery':'500','other':0,'overheadPct':'10','markupPct':'20','vatPct':0}}
    result=calculate(project,[{'id':'m','name':'Плитка','unit':'м²','price':'1200','source':'Тестовый прайс'}])
    assert result['summary']['materials']==13200
    assert result['summary']['labor']==6000
    assert result['summary']['direct']==20700
    assert result['summary']['overheads']==2070
    assert result['summary']['cost']==22770
    assert result['summary']['profit']==4554
    assert result['summary']['total']==27324
    assert result['incomplete'] is False


def test_missing_prices_not_invented():
    result=calculate({'lines':[{'name':'Работа','unit':'м','qty':'12','laborRate':None,'materialRequired':True,'materials':[]}]},[])
    assert result['summary']['total']==0
    assert result['incomplete']
    assert len(result['issues']) >= 2


def test_xlsx_roundtrip():
    book=[{'name':'ВОР','rows':[['Наименование работ','Ед. изм.','Количество'],['Штукатурка','м²',123.45],['Кладка','м³',12.7]]},
          {'name':'Материалы','rows':[['Материал','Цена'],['Кирпич',1200]]}]
    data=create_xlsx(book)
    restored=parse_xlsx(data)
    assert [s['name'] for s in restored] == ['ВОР','Материалы']
    assert restored[0]['rows'][1] == ['Штукатурка', 'м²', '123.45']
    assert restored[1]['rows'][1] == ['Кирпич', '1200']


def test_empty_vor_is_incomplete():
    res = calculate({'lines': []}, [])
    assert res['incomplete']
    assert any('пуста' in issue for issue in res['issues'])


def test_merge_same_material_and_unit_with_different_prices():
    p={'lines':[{'name':'Штукатурка','unit':'м²','qty':12,'source':'Лист 1','laborRate':500,'materialRequired':True,
                 'materials':[{'materialId':'a','norm':2,'waste':0},{'materialId':'b','norm':1,'waste':0}]}]}
    c=[{'id':'a','name':'Смесь','unit':'кг','price':10,'source':'Прайс 1'},
       {'id':'b','name':'смесь','unit':'кг','price':12,'source':'Прайс 2'}]
    res=calculate(p,c)
    assert len(res['resources'])==1
    assert res['resources'][0]['quantity']==36
    assert res['resources'][0]['cost']==384
    assert res['resources'][0]['mixedPrices']
    assert not res['incomplete']
