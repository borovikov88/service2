"""CSRF-protected controls within the existing Avito workspace."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .avito_management import _scope
from .communication_models import ChannelConnection
from .avito_daily_statistics import configure, collect_next, ACCOUNT


@login_required
@never_cache
@require_POST
def control(request, connection_id):
    organization = _scope(request)
    connection = ChannelConnection.objects.select_related('channel').filter(
        pk=connection_id, external_id=ACCOUNT, channel__organization=organization,
        channel__kind='avito').first()
    if connection_id != 4 or connection is None:
        raise Http404
    action = request.POST.get('action')
    if action not in {'enable', 'disable', 'collect'}:
        messages.error(request, 'Неизвестное действие статистики.')
    else:
        try:
            configure(connection, request.user, 'retry' if action == 'collect' else action)
            if action == 'collect':
                status = collect_next(connection_id)
                messages.info(request, {
                    'success': 'Суточные факты сохранены. Часовой пояс источника по-прежнему не подтверждён.',
                    'skipped': 'Сбор ожидает следующую страницу, лимит API или свободный обработчик.',
                    'failed': 'Статистика не подтверждена. Проверьте причину в истории эффективности.',
                }[status])
            else:
                messages.info(request, 'Настройка фонового сбора сохранена. Это не подтверждение запуска.')
        except ValueError as error:
            messages.error(request, str(error))
    return redirect(reverse('avito_dashboard') + '?account=4#avito-analytics-details')
