"""Scoped project preparation; this screen never applies or publishes a change."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .avito_services_feed import connection_for
from . import avito_improvement_proposals as projects


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def service_improvement_proposals(request, connection_id):
    connection = connection_for(request, connection_id)
    if connection.pk != 4 or str(connection.external_id) != projects.ACCOUNT:
        raise PermissionDenied
    result = None
    code = ''
    if request.method == 'POST':
        if request.POST.get('action') != 'prepare':
            return HttpResponse('Неизвестное действие.', status=400)
        try:
            if not request.POST.get('revision') or not request.POST.get('source_version'):
                raise projects.ProposalError('revision_changed')
            result = projects.prepare(connection, request.user,
                expected_revision=request.POST.get('revision'),
                expected_source=request.POST.get('source_version'))
        except projects.ProposalError as error:
            code = str(error) if str(error) in projects.ERRORS else 'unavailable'
    try:
        context = projects.page_context(connection, request.user)
    except projects.ProposalError as error:
        code = str(error) if str(error) in projects.ERRORS else 'unavailable'
        context = {'connection': connection, 'projects': [], 'error': ''}
    if result:
        for row in result['results']:
            row['reason_label'] = projects.SKIPS.get(row['code'], '')
    context.update(result=result, error=projects.ERRORS.get(code, ''))
    return render(request, 'pool_service/avito/improvement_proposals.html', context,
                  status=409 if code == 'revision_changed' else 400 if code else 200)
