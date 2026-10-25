"""Confirmed public stock summaries, separate from permission to backorder."""
import re
from html import unescape

STATES = {'own_stock', 'supplier_stock', 'preorder', 'unavailable', 'unknown'}
CONFIRMED = {'own_stock', 'supplier_stock'}


def clean(value):
    return ' '.join(unescape(re.sub(r'<[^>]*>', ' ', value or '')).split())


def parse(offer):
    states = [clean(p.text).casefold() for p in offer.findall('param')
              if clean(p.get('name')).casefold() == 'наличие для подбора']
    labels = list(dict.fromkeys(clean(p.text) for p in offer.findall('param')
                               if clean(p.get('name')).casefold() == 'наличие'))
    issue = ''
    state = 'unknown'
    if states:
        if any(s not in STATES for s in states) or len(set(states)) != 1:
            issue = 'Противоречивые или неизвестные сведения о наличии в выгрузке магазина.'
        else:
            state = states[0]
    if len(labels) > 1:
        issue = 'Противоречивые публичные статусы наличия в выгрузке магазина.'
    return {'state': state, 'label': '; '.join(labels)[:300], 'issue': issue,
            'orderable': offer.get('available') == 'true'}


def confirmed(facts):
    return bool(facts.get('orderable') and not facts.get('issue') and facts.get('state') in CONFIRMED)


def reason(product):
    facts = product.get('availability') or {}
    if not facts:
        return 'Выгрузка магазина не подтверждает доступность к заказу.'
    if facts.get('issue'):
        return facts['issue']
    state = facts.get('state')
    if state == 'preorder':
        return 'Товар под заказ: наличие на складе магазина или у поставщика не подтверждено; в автоподбор не включается.'
    if state == 'unavailable':
        return 'Товара нет в наличии на складе магазина и у поставщика; в автоподбор не включается.'
    if state not in CONFIRMED:
        return 'Наличие на складе магазина или у поставщика не подтверждено. Статуса «доступен к заказу» недостаточно для автоподбора.'
    if not facts.get('orderable'):
        return 'Магазин не подтверждает доступность товара к заказу.'
    return None if product.get('available') else 'Доступность товара не подтверждена текущей выгрузкой магазина.'


def normalize(raw):
    from .pool_estimates import boolean, string
    if not isinstance(raw, dict) or not raw:
        return {}
    state = string(raw.get('state', 'unknown'), 'Наличие товара', 30)
    if state not in STATES:
        state = 'unknown'
    return {'state': state, 'label': string(raw.get('label', ''), 'Статус наличия', 300),
            'issue': string(raw.get('issue', ''), 'Проверка наличия', 500),
            'orderable': boolean(raw.get('orderable', False))}
