"""Shared OAuth topology for the Finance and 1C Diagnostic MCP resources.

ChatGPT already connects successfully to the Finance MCP authorization server.
Keep that exact issuer and endpoint topology as the single authorization server,
then dispatch authorization/token requests by the RFC 8707 ``resource`` value.
The two MCPs still keep separate scopes, resource audiences, authorization
policies, grants, token validation and audit paths.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.http import HttpResponse, HttpResponseNotFound, HttpResponseServerError, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from pool_service import finance_mcp_views
from pool_service import onec_diagnostic_mcp_chatgpt as diagnostic_chatgpt
from pool_service import onec_diagnostic_mcp_views as diagnostic_views
from pool_service.finance_mcp_auth import (
    FinanceMcpConfigurationError,
    authorization_server_metadata as finance_authorization_server_metadata_data,
    is_enabled as finance_is_enabled,
    issuer_url as finance_issuer_url,
)
from pool_service.onec_diagnostic_mcp_auth import (
    DIAGNOSTIC_READ_SCOPE,
    OneCDiagnosticMcpConfigurationError,
    is_enabled as diagnostic_is_enabled,
    resource_url as diagnostic_resource_url,
)


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def shared_authorization_server_metadata_data():
    """Return the proven Finance AS metadata extended with Diagnostic scope."""
    data = dict(finance_authorization_server_metadata_data())
    scopes = list(data.get("scopes_supported") or [])
    if diagnostic_is_enabled() and DIAGNOSTIC_READ_SCOPE not in scopes:
        scopes.append(DIAGNOSTIC_READ_SCOPE)
    data["scopes_supported"] = scopes
    return data


def diagnostic_protected_resource_metadata_data():
    """Bind Diagnostic resource discovery to the same issuer as Finance MCP."""
    try:
        issuer = finance_issuer_url()
    except FinanceMcpConfigurationError as exc:
        raise OneCDiagnosticMcpConfigurationError(
            "Shared MCP authorization issuer is not configured."
        ) from exc
    return {
        "resource": diagnostic_resource_url(),
        "authorization_servers": [issuer],
        "scopes_supported": [DIAGNOSTIC_READ_SCOPE],
        "bearer_methods_supported": ["header"],
    }


def diagnostic_authorization_redirect_uri(
    redirect_uri,
    *,
    code=None,
    state=None,
    error=None,
    error_description=None,
):
    """Build a Diagnostic OAuth response with the shared root issuer in ``iss``."""
    parts = urlsplit(redirect_uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if code is not None:
        query["code"] = code
    if error is not None:
        query["error"] = error
    if error_description is not None:
        query["error_description"] = error_description
    if state is not None:
        query["state"] = state
    try:
        query["iss"] = finance_issuer_url()
    except FinanceMcpConfigurationError as exc:
        raise OneCDiagnosticMcpConfigurationError(
            "Shared MCP authorization issuer is not configured."
        ) from exc
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


# The Diagnostic implementation already centralizes resource/scope validation,
# owner/accountant policy and token isolation. Only its public issuer topology is
# replaced here. Module globals are resolved at call time, so this keeps the
# reviewed implementation intact while making ChatGPT see the proven Finance AS.
diagnostic_chatgpt.protected_resource_metadata = diagnostic_protected_resource_metadata_data
diagnostic_views.protected_resource_metadata = diagnostic_protected_resource_metadata_data
diagnostic_views.authorization_redirect_uri = diagnostic_authorization_redirect_uri


def shared_authorization_server_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    if not finance_is_enabled() and not diagnostic_is_enabled():
        return _no_store(HttpResponseNotFound())
    try:
        return _no_store(JsonResponse(shared_authorization_server_metadata_data()))
    except (FinanceMcpConfigurationError, OneCDiagnosticMcpConfigurationError):
        return _no_store(HttpResponseServerError())


def diagnostic_protected_resource_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    if not diagnostic_is_enabled():
        return _no_store(HttpResponseNotFound())
    try:
        return _no_store(JsonResponse(diagnostic_protected_resource_metadata_data()))
    except (FinanceMcpConfigurationError, OneCDiagnosticMcpConfigurationError):
        return _no_store(HttpResponseServerError())


def _single_resource(query_dict):
    values = query_dict.getlist("resource")
    return values[0] if len(values) == 1 else None


def _is_diagnostic_resource(value):
    try:
        return value == diagnostic_resource_url()
    except OneCDiagnosticMcpConfigurationError:
        return False


@require_http_methods(["GET", "POST"])
def shared_oauth_authorize(request):
    """Dispatch the shared authorization endpoint by exact requested resource."""
    params = request.GET if request.method == "GET" else request.POST
    if _is_diagnostic_resource(_single_resource(params)):
        return diagnostic_views.onec_diagnostic_oauth_authorize(request)
    return finance_mcp_views.finance_oauth_authorize(request)


@csrf_exempt
@require_http_methods(["POST"])
def shared_oauth_token(request):
    """Dispatch the shared token endpoint by exact requested resource."""
    if _is_diagnostic_resource(_single_resource(request.POST)):
        return diagnostic_views.onec_diagnostic_oauth_token(request)
    return finance_mcp_views.finance_oauth_token(request)


__all__ = [
    "diagnostic_authorization_redirect_uri",
    "diagnostic_protected_resource_metadata",
    "diagnostic_protected_resource_metadata_data",
    "shared_authorization_server_metadata",
    "shared_authorization_server_metadata_data",
    "shared_oauth_authorize",
    "shared_oauth_token",
]
