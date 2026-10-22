"""Explicit roll dimensions and sale units; thickness is never a roll dimension."""
from decimal import Decimal
import re
from .estimate_catalog_params import text
N=r'(\d+(?:[.,]\d+)?)'


def decimal(value):
    try:
        d=Decimal(str(value).replace(',','.'))
        return d if d.is_finite() and 0<d<=100000 else None
    except Exception:return None


def metres(value,default=''):
    m=re.fullmatch(N+r'\s*(мм|см|м|mm|cm|m)?',value)
    if not m:return None
    unit=m[2] or default
    if unit not in {'мм','см','м','mm','cm','m'}:return None
    return decimal(m[1]) / (1000 if unit in {'мм','mm'} else 100 if unit in {'см','cm'} else 1) if decimal(m[1]) else None


def classify(specs,name,description,category,offer):
    title=text(name)
    if not re.match(r'^(?:(?:армированн\w*|пвх|бассейнов\w*)[ -]+)*(?:лайнер|пленка|пвх-мембрана)\b',title):return
    specs['kind']='liner'
    claims={'width':set(),'length':set(),'area':set(),'unit':set()};invalid=False
    sources=[]
    def add(key,value,source):
        nonlocal invalid
        if value is None:invalid=True
        else:claims[key].add(str(value.normalize()) if isinstance(value,Decimal) else value);sources.append(source)
    for param in offer.findall('param'):
        label,value=text(param.get('name')),text(param.text)
        for key,word in [('width','ширина'),('length','длина')]:
            if re.fullmatch(word+r'(?:\s+(?:рулона|пленки|лайнера))?(?:\s*[,()]\s*(?:м|см|мм)\s*\)?)?',label):
                unit=re.search(r'(?:[,()]\s*)(мм|см|м)\s*\)?$',label)
                add(key,metres(value,unit[1] if unit else ''),'XML: '+label)
        if label in {'площадь рулона','площадь рулона, м²','площадь рулона, м2'}:
            m=re.fullmatch(N+r'\s*(?:м²|м2|m2)?',value);add('area',decimal(m[1]) if m else None,'XML: '+label)
        if label in {'единица цены','единица продажи','единица измерения'}:
            add('unit','m2' if value in {'м²','м2','m²','m2','кв.м','кв. м'} else 'roll' if value in {'рулон','рул.','рул','шт','шт.'} else 'unknown','XML: '+label)
    # Two-dimensional size with explicit common units, e.g. 1.65x25.2 m.
    # Reject three-number tuples: 1.65x25x1.5 mm does not prove mixed units.
    for source in (title,text(description)):
        pattern=r'(?<![\d.,xх×])'+N+r'\s*[xх×*]\s*'+N+r'\s*(мм|см|м|mm|cm|m)\b'
        for m in re.finditer(pattern,source):
            if re.search(r'[xх×*]\s*$',source[:m.start()]) or re.match(r'\s*[xх×*]',source[m.end():]):continue
            # Only title or a locally labelled roll size in descriptions.
            if source!=title and not re.search(r'(?:размер\w*\s+(?:рулона|пленки|лайнера)|размер\w*\s*[:=])\s*[:=]?\s*$',source[:m.start()]):continue
            dims=[metres(m[1]+m[3]),metres(m[2]+m[3])]
            if None in dims:invalid=True;continue
            add('width',min(dims),'Размер рулона: '+m[0]);add('length',max(dims),'Размер рулона: '+m[0])
    desc=text(description)
    for key,word in [('width','ширина'),('length','длина')]:
        for m in re.finditer(word+r'(?:\s+(?:рулона|пленки|лайнера))?\s*[:=]\s*'+N+r'\s*(мм|см|м)\b',desc):
            add(key,metres(m[1]+m[2]),'Описание: '+m[0])
    for m in re.finditer(r'площадь рулона\s*[:=]?\s*'+N+r'\s*(?:м²|м2)\b',title+'; '+desc):add('area',decimal(m[1]),'Площадь рулона: '+m[0])
    if claims['width'] and claims['length'] and len(claims['width'])==len(claims['length'])==1:
        a=Decimal(next(iter(claims['width'])))*Decimal(next(iter(claims['length'])))
        claims['area'].add(str(a.normalize()))
    conflict=invalid or any(len(v)>1 for v in claims.values())
    values={key:next(iter(v)) if len(v)==1 else None for key,v in claims.items()}
    # The existing feed prices liners per roll. A labelled pair of complete
    # roll dimensions, or a confirmed SKU, is required before conversion.
    unit=values['unit'] or ('roll' if values['area'] else 'unknown')
    wrong=bool(re.search(r'для\s+(?:кровл\w*|теплиц\w*|пароизоляци\w*)',title+' '+text(description)))
    specs['liner']={'width':values['width'],'length':values['length'],'area':values['area'],'price_unit':unit,
        'unit_explicit':bool(claims['unit']),'conflict':bool(conflict),'wrong_purpose':wrong,'source':'; '.join(dict.fromkeys(sources))[:500]}


def normalize(raw):
    from .pool_estimates import EstimateInputError
    if raw is None or raw=={}:return {}
    if not isinstance(raw,dict) or raw.get('price_unit') not in {'roll','m2','unknown'}:raise EstimateInputError('Некорректная единица продажи лайнера.')
    out={}
    for key in ('width','length','area'):
        value=raw.get(key)
        if value is not None and decimal(value) is None:raise EstimateInputError('Некорректный размер рулона.')
        out[key]=format(decimal(value).normalize(),'f') if value is not None else None
    if out['width'] and out['length'] and out['area'] and Decimal(out['width'])*Decimal(out['length'])!=Decimal(out['area']):raise EstimateInputError('Площадь рулона не соответствует его размерам.')
    for key in ('conflict','wrong_purpose','unit_explicit'):
        if not isinstance(raw.get(key,False),bool):raise EstimateInputError('Некорректные характеристики лайнера.')
        out[key]=raw.get(key,False)
    out['price_unit']=raw['price_unit']
    from .pool_estimates import string
    out['source']=string(raw.get('source',''),'Источник размера рулона',500)
    return out
