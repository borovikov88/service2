"""Role templates shared by the Service2 access review. A template is not a grant."""
from dataclasses import dataclass

ROLES = {
    'owner': 'Директор', 'sales_head': 'РОП', 'manager': 'Менеджер',
    'service_head': 'Руководитель сервиса', 'installer': 'Мастер / монтажник',
    'avito_operator': 'Оператор Avito', 'cashier': 'Кассир',
    'accountant': 'Бухгалтер', 'financier': 'Финансист', 'technical_admin': 'Технический администратор',
}
SCOPES = {
    'none': 'Закрыто', 'organization': 'В своей организации', 'team': 'Свои и закреплённый отдел',
    'own': 'Только свои', 'assigned': 'На назначенных объектах', 'queue': 'Свободные в своей очереди',
    'responsible': 'Текущий ответственный за основную карточку', 'register': 'В назначенном регистре',
    'account': 'В назначенном аккаунте', 'explicit': 'Требуется отдельное назначение',
    'participating': 'При подтверждённом участии', 'self': 'Только для себя',
}


@dataclass(frozen=True)
class Rule:
    key: str
    module: str
    operation: str
    roles: dict
    condition: str = ''


RULES = (
    Rule('sales.read', 'Лиды и сделки', 'Читать рабочие карточки',
         {'owner': 'organization', 'sales_head': 'team', 'manager': 'organization'}, 'Приватный источник проверяется отдельно.'),
    Rule('sales.create', 'Лиды и сделки', 'Создать и назначить ответственного',
         {'owner':'organization','sales_head':'team','manager':'own'}),
    Rule('sales.edit', 'Лиды и сделки', 'Изменить поля, этап, завершить или конвертировать',
         {'owner':'organization','sales_head':'team','manager':'own'}),
    Rule('sales.assign', 'Лиды и сделки', 'Распределить карточки',
         {'owner':'organization','sales_head':'team','manager':'queue'}, 'Менеджер может только взять свободную карточку.'),
    Rule('sales.transfer', 'Лиды и сделки', 'Передать карточку в другой отдел', {'owner':'organization'}),
    Rule('sales.request_transfer', 'Лиды и сделки', 'Запросить передачу в другой отдел', {'sales_head':'team','manager':'own'}),
    Rule('sales.request_archive', 'Лиды и сделки', 'Запросить архивирование или восстановление', {'manager':'own'}),
    Rule('sales.archive', 'Лиды и сделки', 'Архивировать или восстановить',
         {'owner':'organization','sales_head':'team'}, 'Для руководителя отдела обязательна причина.'),
    Rule('sales.export', 'Лиды и сделки', 'Выгрузить список', {'owner':'explicit'}),
    Rule('calls.work', 'Телефония', 'Слушать обычный рабочий разговор',
         {'owner':'organization','sales_head':'team','manager':'own'},
         'Нужен допуск к записям. Менеджер слушает свои разговоры и разговоры по своим карточкам.'),
    Rule('calls.leader', 'Телефония', 'Слушать разговор директора или РОПа',
         {'owner':'organization','sales_head':'responsible','manager':'responsible'},
         'Нужен допуск к содержанию. РОП также слушает собственные рабочие звонки.'),
    Rule('calls.internal', 'Телефония', 'Слушать внутренний разговор', {'owner':'organization',
         'sales_head':'participating','manager':'participating','installer':'participating'},
         'Нужен допуск к записям. Защищённая личная история исключена из рабочего журнала.'),
    Rule('calls.transcript', 'Телефония', 'Читать разрешённую расшифровку',
         {'owner':'organization','sales_head':'team','manager':'own'},
         'Проверяется конкретный разговор. Для звонка вышестоящего руководителя нужны допуск и ответственность за основную карточку.'),
    Rule('calls.download', 'Телефония', 'Скачать запись', {'owner':'explicit'}),
    Rule('calls.export_text', 'Телефония', 'Экспортировать расшифровки', {'owner':'explicit'}),
    Rule('calls.recognize', 'Телефония', 'Запустить распознавание', {'owner':'explicit'}),
    Rule('calls.private', 'Телефония', 'Подтвердить личного собеседника', {'owner':'self'}),
    Rule('messages.reply', 'Диалоги', 'Ответить клиенту', {'owner':'organization','sales_head':'team','manager':'assigned'}),
    Rule('messages.take', 'Диалоги', 'Взять свободное обращение', {'owner':'organization','sales_head':'team','manager':'queue'}),
    Rule('messages.bind', 'Диалоги', 'Изменить привязку к карточке', {'owner':'explicit','sales_head':'explicit','manager':'explicit'}),
    Rule('service.plan', 'Объекты и выезды', 'Планировать работы отдела', {'owner':'organization','service_head':'team'}),
    Rule('service.result', 'Объекты и выезды', 'Внести выезд, замеры, фотографии и результат',
         {'owner':'organization','service_head':'team','installer':'assigned'}),
    Rule('tasks.read', 'Задачи и календарь', 'Открыть рабочее задание',
         {'owner':'organization','sales_head':'team','manager':'assigned','service_head':'team','installer':'assigned'},
         'Личные задачи и материалы звонков сохраняют собственные ограничения.'),
    Rule('clients.use', 'Контакты', 'Использовать рабочий контакт', {'owner':'organization','sales_head':'team',
         'manager':'organization','installer':'assigned'}),
    Rule('clients.merge', 'Контакты', 'Объединить карточки', {'owner':'explicit'}),
    Rule('clients.export', 'Контакты', 'Экспортировать базу', {'owner':'explicit'}),
    Rule('quotes.edit', 'Сметы', 'Готовить сметы', {'owner':'organization','sales_head':'team','manager':'own'}),
    Rule('quotes.cost', 'Сметы и каталог', 'Видеть себестоимость', {'owner':'explicit','financier':'explicit'}),
    Rule('quotes.pricing', 'Сметы и каталог', 'Изменять ценовые правила', {'owner':'explicit'}),
    Rule('avito.edit', 'Каталог и Avito', 'Редактировать объявления и фото', {'owner':'explicit','avito_operator':'account'}),
    Rule('avito.publish', 'Каталог и Avito', 'Публиковать объявления', {'owner':'explicit','avito_operator':'explicit'}),
    Rule('avito.export', 'Каталог и Avito', 'Выполнить массовую выгрузку', {'owner':'explicit','avito_operator':'explicit'}),
    Rule('avito.spend', 'Каталог и Avito', 'Управлять платным продвижением', {'owner':'explicit','avito_operator':'explicit'}),
    Rule('tenders.prepare', 'Торги', 'Вести закупку и готовить материалы', {'owner':'organization','sales_head':'team','manager':'own'}),
    Rule('tenders.approve', 'Торги', 'Утвердить цену заявки', {'owner':'explicit'}),
    Rule('tenders.submit', 'Торги', 'Подать заявку', {'owner':'explicit','manager':'explicit'}),
    Rule('cash.enter', 'Деньги', 'Внести рабочую операцию', {'owner':'assigned','cashier':'register',
         'accountant':'register','manager':'own','installer':'own'}),
    Rule('cash.confirm', 'Деньги', 'Подтвердить или исправить операцию', {'owner':'explicit','accountant':'explicit'}),
    Rule('finance.register', 'Деньги и аналитика', 'Читать назначенный регистр', {'owner':'explicit','accountant':'register','financier':'register'}),
    Rule('finance.report', 'Деньги и аналитика', 'Читать управленческую отчётность', {'owner':'explicit','accountant':'explicit','financier':'explicit'},
         'Назначение ограничивается допущенными организациями и регистрами.'),
    Rule('finance.payroll', 'Деньги и аналитика', 'Открыть ФОТ и персональные начисления', {'owner':'explicit','accountant':'explicit'}),
    Rule('onec.read', '1С', 'Читать подтверждённые данные', {'owner':'explicit','financier':'register','accountant':'register'}),
    Rule('onec.import', '1С', 'Запустить импорт', {'owner':'explicit','financier':'explicit'}),
    Rule('onec.confirm', '1С', 'Подтвердить импортированные данные', {'owner':'explicit','financier':'explicit'}),
    Rule('onec.export', '1С', 'Экспортировать данные', {'owner':'explicit','financier':'explicit','accountant':'explicit'}),
    Rule('onec.staff_detail', '1С', 'Открывать детализацию по сотруднику', {'owner':'explicit','financier':'explicit','accountant':'explicit'}),
    Rule('onec.settings', '1С', 'Настраивать интеграцию', {'owner':'explicit','technical_admin':'explicit'}),
    Rule('staff.owner', 'Сотрудники и права', 'Изменить владельца организации', {'owner':'explicit'}),
    Rule('staff.structure', 'Сотрудники и права', 'Создавать отделы и назначать руководителей', {'owner':'organization'}),
    Rule('staff.roles', 'Сотрудники и права', 'Назначать права', {'owner':'explicit'}),
    Rule('integrations.manage', 'Подключения', 'Обслуживать подключение', {'owner':'explicit','technical_admin':'assigned'},
         'Содержимое рабочих разговоров и переписки проверяется отдельно.'),
)

RULE_BY_KEY = {rule.key: rule for rule in RULES}


def role_scope(role, action):
    rule = RULE_BY_KEY.get(action)
    return rule.roles.get(role, 'none') if rule else 'none'


def role_rows(role):
    return [{'module': rule.module, 'operation': rule.operation, 'scope': SCOPES[role_scope(role, rule.key)],
             'condition': rule.condition} for rule in RULES]
