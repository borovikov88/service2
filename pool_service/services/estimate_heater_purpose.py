"""Explicit home-heating evidence is not missing pool compatibility data."""
import re
from html import unescape
from urllib.parse import unquote


def purpose_note(name='', category='', url='', description=''):
    # Scope the evidence to an explicit domestic/building heating purpose.
    # A bare "воздух–вода", "отопление" or mention of a pool proves neither use.
    purpose = (r'\bдля\s+(?:(?:отопления|обогрева)\s+)?'
               r'(?:дом(?:а|ов)\b|коттедж\w*|здани\w*|помещени\w*|жилищ\w*)'
               r'|\b(?:отоплени[ея]|обогрев[а]?)\s+(?:жил\w*\s+)?'
               r'(?:дом(?:а|ов)\b|коттедж\w*|здани\w*|помещени\w*)')
    for source, raw in (('название', name), ('категория', category), ('описание', description)):
        text = unescape(re.sub(r'<[^>]*>', ' ', raw or '')).casefold().replace('ё', 'е')
        if source == 'категория':
            # An ancestor "Товары для дома" is not a heating-system category.
            text = ' / '.join(part for part in text.split('/') if re.search(
                r'теплов\w*\s+насос|отоплен|обогрев', part))
        # Ignore only a negated domestic claim, not another restriction in the
        # same clause (e.g. "для дома, не подходит для бассейна").
        for clause in re.split(r'(?<!\d)\.|\.(?!\d)|[;!?\n]', text):
            for match in re.finditer(purpose, clause):
                if re.search(r'\bне\s+(?:(?:предназначен\w*|подходит|используется|применяется)\s+)?$', clause[:match.start()]):
                    continue
                # "отопления дома" also occurs inside a negated "для ..." claim.
                if re.search(r'\bне\s+(?:(?:предназначен\w*|подходит|используется|применяется)\s+)?для\s+$', clause[:match.start()]):
                    continue
                return source + ': ' + re.sub(r'\s+', ' ', clause).strip()[:220]
    path = unquote(url or '').casefold()
    if re.search(r'(?:teplov[^/]*nasos[^/]*|nasos[^/]*)-dlya-(?:(?:otopleniya|obogreva)-)?(?:doma|domov|kottedzha|zdaniy|pomeshcheniy)(?:[-/.]|$)', path):
        return 'URL: раздел/товар тепловых насосов для дома или отопления'
    return ''


def reason(product):
    if product.get('heater_type') != 'heatpump':
        return None
    evidence = product.get('heater_purpose_note') or purpose_note(
        product.get('name', ''), product.get('category', ''), product.get('url', ''),
        product.get('description', ''))
    if not evidence:
        return None
    return ('Модель для отопления дома/здания: прямой нагрев воды бассейна не подтверждён. '
            'Для применения через отдельный теплообменник нужна подтверждённая схема; '
            'автоматический подбор в прямой бассейновый контур запрещён. Источник: ' + evidence + '.')
