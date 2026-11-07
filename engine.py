"""Смеч: deterministic contractor-side estimate engine (NOT an official Russian normative estimate).

Each material link can hold a project-specific immutable price, unit, name and source
snapshot. Legacy v0.1 links continue to work; client migration freezes their price.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
import re
from typing import Any

MONEY = Decimal('0.01')
QTY = Decimal('0.001')
DISCRETE = {'шт', 'компл', 'комплект', 'упак'}
MAX_NUMBER = Decimal('1000000000000')
MAX_TOTAL = Decimal('1000000000000000')


def number(value: Any, *, optional: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None if optional else Decimal('0')
    if isinstance(value, bool):
        raise ValueError('Логическое значение вместо числа')
    s = str(value).strip().replace('\u00a0', ' ').replace('\u202f', ' ')
    if ' ' in s:
        if not re.fullmatch(r'\d{1,3}(?: \d{3})+(?:[.,]\d+)?', s):
            raise ValueError(f'Некорректные разделители в числе: {value}')
        s = s.replace(' ', '')
    if not re.fullmatch(r'\d+(?:[.,]\d+)?', s):
        raise ValueError(f'Некорректное неотрицательное число: {value}')
    try:
        n = Decimal(s.replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError(f'Некорректное число: {value}') from exc
    if len(re.split(r'[.,]', s)[-1]) > 12 and ('.' in s or ',' in s):
        raise ValueError('Допускается не более 12 знаков после запятой')
    if not n.is_finite() or n > MAX_NUMBER:
        raise ValueError('Число выходит за допустимый диапазон (до 1 трлн)')
    return n


def rub(v: Decimal) -> Decimal:
    if not v.is_finite() or v > MAX_TOTAL or v < 0:
        raise ValueError('Итоговая сумма выходит за допустимый диапазон')
    return v.quantize(MONEY, rounding=ROUND_HALF_UP)


def amount(v: Decimal) -> float:
    return float(rub(v))


def needed(v: Decimal, unit: str) -> Decimal:
    quantum = Decimal('1') if str(unit).strip().lower() in DISCRETE else QTY
    return v.quantize(quantum, rounding=ROUND_CEILING)


def calculate(project: dict, catalog: list[dict]) -> dict:
    if not isinstance(project, dict) or not isinstance(catalog, list):
        raise ValueError('Неверная структура входных данных')
    catalog_by_id = {str(m.get('id')): m for m in catalog if isinstance(m, dict)}
    blockers, warnings = [], []
    positions, consolidated = [], {}
    material_total = labor_total = Decimal(0)
    lines = project.get('lines', [])
    if not isinstance(lines, list) or len(lines) > 10000:
        raise ValueError('Ведомость должна содержать не более 10 000 строк')
    if not lines:
        blockers.append('Ведомость пуста — добавьте хотя бы одну позицию работ')

    fingerprints = {}
    for i, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            blockers.append(f'Позиция {i}: повреждённая строка')
            continue
        name = str(line.get('name') or '').strip()
        title = name or f'Строка {i}'
        unit = str(line.get('unit') or '').strip()
        if not name:
            blockers.append(f'Позиция {i}: не указано наименование работы')
        if not unit:
            blockers.append(f'Позиция {i} «{title}»: нет единицы измерения')
        if not str(line.get('source') or '').strip():
            blockers.append(f'Позиция {i} «{title}»: нет источника/листа объёма')
        fingerprint = (name.casefold(), unit.casefold(), str(line.get('section') or '').casefold(), str(line.get('code') or '').casefold(), str(line.get('source') or '').casefold())
        if name and fingerprint in fingerprints:
            warnings.append(f'Позиции {fingerprints[fingerprint]} и {i}: возможный дубль работы «{title}» (объёмы сохранены)')
        fingerprints[fingerprint] = i

        try:
            qty = number(line.get('qty'), optional=True)
            rate = number(line.get('laborRate'), optional=True)
        except ValueError as e:
            blockers.append(f'Позиция {i} «{title}»: {e}')
            qty, rate = None, None
        if qty is None or qty == 0:
            blockers.append(f'Позиция {i} «{title}»: не задан положительный объём')
        if rate is None or rate == 0:
            blockers.append(f'Позиция {i} «{title}»: нет положительной сдельной ставки оплаты СМР')
        if rate is not None and rate > 0 and not str(line.get('laborSource') or '').strip():
            warnings.append(f'Позиция {i} «{title}»: не указан источник сдельной ставки')
        if line.get('materialRequired') not in (True, False) or not isinstance(line.get('materialRequired'), bool):
            blockers.append(f'Позиция {i} «{title}»: не определено, нужны ли материалы')
        raw_links = line.get('materials') or []
        if not isinstance(raw_links, list) or len(raw_links) > 200:
            raise ValueError(f'Позиция {i}: слишком много или неверный формат привязок')
        if line.get('materialRequired') is True and not raw_links:
            blockers.append(f'Позиция {i} «{title}»: материалы требуются, но нормы не привязаны')
        if line.get('materialRequired') is False and raw_links:
            warnings.append(f'Позиция {i} «{title}»: указано «без материалов», но имеются привязки')
        qsafe = qty if qty is not None else Decimal(0)
        labor = rub(qsafe * rate) if rate is not None else Decimal(0)
        labor_total += labor
        material_cost = Decimal(0)
        consumptions = []
        for link in raw_links:
            if not isinstance(link, dict):
                blockers.append(f'Позиция {i} «{title}»: повреждённая привязка материала')
                continue
            mat = catalog_by_id.get(str(link.get('materialId')), {})
            # Snapshot fields, once present, override the changeable shared catalog.
            mname = str(link.get('materialName', mat.get('name', ''))).strip()
            munit = str(link.get('materialUnit', mat.get('unit', ''))).strip()
            msource = str(link.get('priceSource', mat.get('source', ''))).strip()
            mdate = str(link.get('priceDate', mat.get('date', ''))).strip()
            raw_price = link['price'] if 'price' in link else mat.get('price')
            if not mname or not munit:
                blockers.append(f'Позиция {i} «{title}»: материал без наименования или единицы')
                continue
            try:
                norm = number(link.get('norm'), optional=True)
                waste = number(link.get('waste', 0))
                price = number(raw_price, optional=True)
                if waste > 100:
                    raise ValueError('потери более 100% требуют отдельного обоснования')
            except ValueError as e:
                blockers.append(f'Позиция {i} «{title}», материал «{mname}»: {e}')
                continue
            if norm is None or norm == 0:
                blockers.append(f'Позиция {i} «{title}», «{mname}»: отсутствует положительная норма расхода')
            if price is None or price == 0:
                blockers.append(f'Материал «{mname}»: нет положительной закупочной цены')
            if price is not None and not msource:
                blockers.append(f'Материал «{mname}»: нет источника цены')
            if price is not None and not mdate:
                warnings.append(f'Материал «{mname}»: не указана дата цены — проверьте актуальность')
            need_qty = needed(qsafe * (norm or Decimal(0)) * (1 + waste / 100), munit)
            subtotal = rub(need_qty * price) if price is not None and norm is not None else Decimal(0)
            material_cost += subtotal
            key = (mname.casefold(), munit.casefold())
            if key not in consolidated:
                consolidated[key] = {'id': link.get('materialId'), 'name': mname, 'unit': munit, 'quantity': Decimal(0),
                                     'cost': Decimal(0), 'price': price, 'missing': False, 'mixedPrices': False, 'origins': set()}
            rec = consolidated[key]
            if rec['price'] != price:
                rec['mixedPrices'] = True
            rec['quantity'] += need_qty
            rec['cost'] += subtotal
            rec['missing'] |= (price is None or not norm or not msource)
            rec['origins'].add(msource or 'нет источника')
            consumptions.append({'material': mname, 'unit': munit, 'quantity': float(need_qty), 'cost': amount(subtotal),
                                 'norm': float(norm) if norm is not None else None, 'waste': float(waste),
                                 'price': float(price) if price is not None else None, 'priceDate': mdate, 'priceSource': msource})
        material_total += material_cost
        positions.append({'id': line.get('id'), 'name': title, 'unit': unit, 'qty': float(qsafe),
                          'materials': amount(material_cost), 'labor': amount(labor),
                          'rate': float(rate) if rate is not None else None,
                          'laborSource': str(line.get('laborSource') or ''), 'laborDate': str(line.get('laborDate') or ''),
                          'consumption': consumptions})

    extras = project.get('extras') or {}
    if not isinstance(extras, dict):
        raise ValueError('Повреждены параметры коммерческого расчёта')
    parsed = {}
    for field in ('machinery', 'delivery', 'other', 'employmentChargesPct', 'overheadPct', 'markupPct', 'vatPct'):
        try:
            parsed[field] = number(extras.get(field, 0))
        except ValueError as e:
            blockers.append(f'Коммерческий расчёт, {field}: {e}')
            parsed[field] = Decimal(0)
    for k in ('employmentChargesPct', 'overheadPct', 'markupPct', 'vatPct'):
        if parsed[k] > 100:
            warnings.append(f'Поле {k}: значение более 100%, проверьте обоснование')
    if parsed['vatPct'] > 30:
        warnings.append('Ставка НДС более 30%: проверьте налоговый режим и правильность ввода')
    mode = extras.get('vatMode', 'rate' if parsed['vatPct'] > 0 else 'none')
    if mode not in ('none', 'rate'):
        blockers.append('Режим НДС не определён: выберите «Без начисления» или «По ставке»')
        mode = 'none'
    employment_charges = rub(labor_total * parsed['employmentChargesPct'] / 100)
    direct = rub(material_total + labor_total + employment_charges + parsed['machinery'] + parsed['delivery'] + parsed['other'])
    overhead = rub(direct * parsed['overheadPct'] / 100)
    cost = rub(direct + overhead)
    profit = rub(cost * parsed['markupPct'] / 100)
    before_vat = rub(cost + profit)
    vat = rub(before_vat * parsed['vatPct'] / 100) if mode == 'rate' else Decimal(0)
    total = rub(before_vat + vat)
    resources = []
    for rec in sorted(consolidated.values(), key=lambda x: str(x['name']).casefold()):
        resources.append({'id': rec['id'], 'name': rec['name'], 'unit': rec['unit'], 'quantity': float(rec['quantity']),
                          'price': None if rec['mixedPrices'] or rec['price'] is None else float(rec['price']),
                          'cost': amount(rec['cost']), 'missing': rec['missing'], 'mixedPrices': rec['mixedPrices'],
                          'sources': sorted(rec['origins'])})
    issues = list(dict.fromkeys(blockers + warnings))
    return {'positions': positions, 'resources': resources, 'issues': issues,
            'blockers': list(dict.fromkeys(blockers)), 'warnings': list(dict.fromkeys(warnings)),
            'incomplete': bool(blockers), 'summary': {
                'materials': amount(material_total), 'labor': amount(labor_total), 'employmentCharges': amount(employment_charges), 'vatMode': mode,
                'machinery': amount(parsed['machinery']), 'delivery': amount(parsed['delivery']),
                'other': amount(parsed['other']), 'direct': amount(direct), 'overheads': amount(overhead),
                'cost': amount(cost), 'profit': amount(profit), 'beforeVat': amount(before_vat),
                'vat': amount(vat), 'total': amount(total),
            }}
