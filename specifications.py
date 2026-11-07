"""Сопоставление спецификаций поставки и расчетной потребности.

Строгое сравнение имени + единицы; никаких предположений о синонимах,
коэффициентах пересчёта, аналогах или фактических остатках на складе.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

MAX_SPEC_ROWS = 10000


def key(value: object) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip().casefold().replace('ё', 'е'))


def quantity(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    t = str(value).strip().replace('\u00a0', ' ').replace('\u202f', ' ')
    if re.fullmatch(r'\d{1,3}(?: \d{3})+(?:[,.]\d{1,6})?', t):
        t = t.replace(' ', '')
    else:
        t = t.replace(' ', '') if ' ' not in t else ''
    t = t.replace(',', '.')
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,6})?', t):
        return None
    try:
        n = Decimal(t)
        return n if n.is_finite() and n > 0 else None
    except InvalidOperation:
        return None


def _print(n: Decimal) -> str:
    return format(n.quantize(Decimal('.001'), rounding=ROUND_HALF_UP), 'f')


def validate_specifications(items: object) -> list[dict]:
    if not isinstance(items, list) or len(items) > MAX_SPEC_ROWS:
        raise ValueError('Неправильное количество строк спецификации')
    seen = set()
    out = []
    for i, obj in enumerate(items, 1):
        if not isinstance(obj, dict):
            raise ValueError(f'Строка {i}: неверная структура')
        ident = obj.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,100}', ident) or ident in seen:
            raise ValueError(f'Строка {i}: неверный или повторяющийся идентификатор')
        seen.add(ident)
        name = obj.get('name')
        unit = obj.get('unit')
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ValueError(f'Строка {i}: отсутствует название материала')
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 40:
            raise ValueError(f'Строка {i}: отсутствует единица измерения')
        qty = quantity(obj.get('qty'))
        if qty is None:
            raise ValueError(f'Строка {i}: количество должно быть положительным числом')
        for f, limit in [('code', 120), ('source', 500), ('note', 1000)]:
            if not isinstance(obj.get(f, ''), str) or len(obj.get(f, '')) > limit:
                raise ValueError(f'Строка {i}: слишком длинное поле {f}')
        out.append({'id': ident, 'name': name.strip(), 'unit': unit.strip(),
                    'qty': str(qty), 'code': obj.get('code', '').strip(),
                    'source': obj.get('source', '').strip(), 'note': obj.get('note', '').strip()})
    return out


def reconcile_specifications(items: object, resources: list[dict]) -> dict:
    specs = validate_specifications(items)
    demands: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    names: dict[tuple[str, str], str] = {}
    for resource in resources:
        material, unit = str(resource.get('name') or ''), str(resource.get('unit') or '')
        if not material.strip() or not unit.strip():
            continue
        k = (key(material), key(unit))
        n = quantity(resource.get('quantity'))
        if n is not None:
            demands[k] += n
            names.setdefault(k, material)
    supplies: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    source_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    spec_names: dict[tuple[str, str], str] = {}
    for s in specs:
        k = (key(s['name']), key(s['unit']))
        supplies[k] += Decimal(s['qty'])
        source_ids[k].append(s['id'])
        spec_names.setdefault(k, s['name'])
    results = []
    stats = {'matched': 0, 'shortage': 0, 'surplus': 0, 'unmatched_spec': 0, 'unmatched_demand': 0}
    for k in sorted(set(demands) | set(supplies)):
        demand = demands.get(k, Decimal(0))
        supply = supplies.get(k, Decimal(0))
        if not demand:
            status = 'unmatched_spec'
        elif not supply:
            status = 'unmatched_demand'
        elif demand == supply:
            status = 'matched'
        elif supply < demand:
            status = 'shortage'
        else:
            status = 'surplus'
        stats[status] += 1
        results.append({'name': names.get(k, spec_names.get(k, '')), 'unit': k[1],
                        'demand': _print(demand), 'specified': _print(supply),
                        'difference': _print(supply - demand), 'status': status,
                        'specLineIds': source_ids.get(k, [])})
    return {'rows': results, 'stats': stats,
            'warning': 'Точное сопоставление наименование + единица. Различающиеся обозначения не объединяются автоматически. Спецификация не подтверждает фактическую закупку.'}
