"""ChatGPT-facing URL binding for the read-only 1C Diagnostic MCP.

The working Finance MCP authenticates every MCP POST before parsing JSON-RPC.
Keep the Diagnostic transport identical to that proven flow, while binding its
OAuth discovery URLs to the canonical Diagnostic resource host instead of the
unrelated application SITE_URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from django.http import JsonResponse

from pool_service import onec_diagnostic_mcp_views as legacy
from pool_service.onec_diagnostic_mcp_auth import (
    authorization_server_metadata,
    issuer_url,
    protected_resource_metadata,
    resource_url,
)


def _origin(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _protected_resource_metadata_url():
    parsed = urlsplit(resource_url())
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/.well-known/oauth-protected-resource/mcp/1c",
        "",
        "",
    ))


# The reviewed transport's Bearer challenge resolves this name from its module
# globals. Point it at the canonical Diagnostic resource host before exposing
# the endpoint. This preserves auth-before-body-parsing exactly like Finance MCP.
legacy.protected_resource_metadata_url = _protected_resource_metadata_url
onec_diagnostic_mcp = legacy.onec_diagnostic_mcp


def onec_diagnostic_protected_resource_metadata(request):
    if request.method != "GET":
        return legacy.onec_diagnostic_protected_resource_metadata(request)
    return legacy._no_store(JsonResponse(protected_resource_metadata()))


def onec_diagnostic_authorization_server_metadata(request):
    if request.method != "GET":
        return legacy.onec_diagnostic_authorization_server_metadata(request)
    data = authorization_server_metadata()
    base = _origin(issuer_url())
    data["authorization_endpoint"] = f"{base}/oauth/1c/authorize"
    data["token_endpoint"] = f"{base}/oauth/1c/token"
    return legacy._no_store(JsonResponse(data))


# OAuth execution itself remains in the reviewed implementation.
onec_diagnostic_oauth_authorize = legacy.onec_diagnostic_oauth_authorize
onec_diagnostic_oauth_token = legacy.onec_diagnostic_oauth_token
