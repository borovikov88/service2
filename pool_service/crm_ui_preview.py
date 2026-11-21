"""Synthetic, read-only CRM presenter and DB-free native template export.

No model imports. Future scoped adapters can supply the same primitive context.
The bootstrap catalogue is parsed as a literal, never executed or queried.
"""
import ast
from pathlib import Path
import shutil

from django.template import Context, Engine

APP = Path(__file__).resolve().parent


def pipeline_catalogue():
    tree = ast.parse((APP / 'crm_commercial_bootstrap.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'PROPOSED_PIPELINES'
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError('Commercial pipeline catalogue missing')


def synthetic_presenter():
    pipelines = [{'code': 'inbox', 'name': 'Входящие', 'stages': [
        {'code': 'new', 'name': 'Новые обращения'},
        {'code': 'clarify', 'name': 'Уточнение потребности'},
        {'code': 'waiting', 'name': 'Ожидаем клиента'},
    ]}]
    pipelines += [{'code': code, 'name': name, 'stages': [
        {'code': stage, 'name': label} for stage, label in stages
    ]} for code, name, stages in pipeline_catalogue()]
    examples = {
        'inbox': [('Бассейн для загородного дома', 'Частный заказчик «Берег»', None),
                  ('Замена фильтрующей загрузки', 'Объект «Сосны»', 48000),
                  ('Оборудование для новой чаши', 'Проект «Лазурь»', None),
                  ('Подготовка бассейна к сезону', 'Объект «Роща»', 65000)],
        'construction': [('Бассейн 8 × 4 с переливом', 'Проект «Лазурь»', 2840000),
                         ('Монтаж системы водоподготовки', 'Объект «Маяк»', 680000),
                         ('Реконструкция чаши', 'Объект «Сосны»', None),
                         ('Крытый бассейн и павильон', 'Проект «Волна»', 4350000),
                         ('Автоматика и подсветка', 'Объект «Берег»', 245000)],
        'service': [('Регулярное обслуживание', 'Комплекс «Горизонт»', 92000),
                    ('Диагностика системы фильтрации', 'Объект «Маяк»', None),
                    ('Обслуживание двух чаш', 'Комплекс «Волна»', 145000),
                    ('Подготовка к зимнему периоду', 'Объект «Роща»', 54000)],
        'retail': [('Комплект для ухода за водой', 'Частный заказчик «Берег»', 18500),
                   ('Робот для очистки бассейна', 'Объект «Сосны»', 128000),
                   ('Насос и запасные комплектующие', 'Проект «Лазурь»', None),
                   ('Фильтрующий материал', 'Комплекс «Горизонт»', 34000)],
        'tender': [('Оборудование спортивного бассейна', 'Учебный объект «Старт»', 5600000),
                   ('Обслуживание водного комплекса', 'Комплекс «Орбита»', None),
                   ('Модернизация водоподготовки', 'Спортивный объект «Вектор»', 3200000),
                   ('Поставка оборудования', 'Учебный объект «Парус»', 890000)],
    }
    owners = [('anna', 'Менеджер Анна'), ('max', 'Менеджер Максим'), ('none', 'Не назначен')]
    sources = [('site', 'Сайт'), ('phone', 'Телефон'), ('object', 'Потребность объекта'), ('tender', 'Закупка')]
    deals = []
    for p_index, pipeline in enumerate(pipelines):
        for index, (title, client, amount) in enumerate(examples[pipeline['code']]):
            stage = pipeline['stages'][index % min(4, len(pipeline['stages']))]
            owner, owner_name = owners[(index + p_index) % len(owners)]
            source, source_name = sources[(index + p_index) % len(sources)]
            if pipeline['code'] == 'tender':
                source, source_name = sources[3]
            status = ['overdue', 'today', 'missing', 'waiting', 'planned'][index]
            step = {'overdue': 'Уточнить параметры объекта', 'today': 'Подготовить расчёт',
                    'missing': 'Следующий шаг не назначен', 'waiting': 'Получить обратную связь',
                    'planned': 'Согласовать состав оборудования'}[status]
            due = {'overdue': 'Просрочено · 23 октября', 'today': 'Сегодня · 26 октября, 15:00',
                   'missing': 'Нужны действие и срок', 'waiting': '28 октября, 11:00',
                   'planned': '29 октября, 10:00'}[status]
            deals.append(dict(id=f'demo-{pipeline["code"]}-{index+1}', title=title, client=client,
                pipeline=pipeline['code'], pipeline_name=pipeline['name'], stage=stage['code'],
                stage_name=stage['name'], owner=owner, owner_name=owner_name,
                source=source, source_name=source_name, status=status, step=step, due=due,
                age=f'{index + 1} дн. на этапе', amount='Не указана' if amount is None else f'{amount:,}'.replace(',', ' ') + ' ₽',
                object_name=f'Демонстрационный объект «{["Берег", "Сосны", "Лазурь", "Роща", "Маяк"][index]}»'))
        pipeline['count'] = len(examples[pipeline['code']])
    for pipeline in pipelines:
        for stage in pipeline['stages']:
            stage['deals'] = [d for d in deals if d['pipeline'] == pipeline['code'] and d['stage'] == stage['code']]
    return {'pipelines': pipelines, 'deals': deals,
            'owners': [{'code': c, 'name': n} for c, n in owners],
            'sources': [{'code': c, 'name': n} for c, n in sources]}


def render_preview(context=None):
    engine = Engine(dirs=[str(APP / 'templates')], autoescape=True)
    return engine.get_template('pool_service/crm_preview/standalone.html').render(
        Context(context if context is not None else synthetic_presenter(), use_l10n=False, use_tz=False))


def export_preview(destination):
    """Export only explicitly selected local assets; no settings or DB required."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    assets = destination / 'assets'
    assets.mkdir(exist_ok=True)
    for name in ('crm-preview.css', 'crm-preview.js'):
        shutil.copyfile(APP / 'static/pool_service/crm_preview' / name, assets / name)
    (destination / 'index.html').write_text(render_preview(), encoding='utf-8')
    return destination.resolve()
