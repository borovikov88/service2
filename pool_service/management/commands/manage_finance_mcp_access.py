"""Owner-operated provisioning and revocation for the finance MCP OAuth client."""

from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pool_service.finance_mcp_auth import (
    CHATGPT_CLIENT_ID_METADATA_URL,
    FinanceMcpConfigurationError,
    fetch_trusted_chatgpt_client_metadata,
    revoke_client,
    revoke_grant,
)
from pool_service.services.finance import can_manage_finance
from pool_service.services.finance_advisor import MAX_ORGANIZATIONS
from pool_service.models import (
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization,
    Organization,
)


def _exact_https_redirect_uri(value):
    parsed = urlsplit(value or "")
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


class Command(BaseCommand):
    help = (
        "Provision a pre-registered public OAuth client for the read-only finance MCP, "
        "or revoke an existing client/grant. It never prints a token or a secret."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("provision", "revoke"))
        parser.add_argument("--client-id")
        parser.add_argument("--client-name")
        parser.add_argument("--principal-subject")
        parser.add_argument("--principal-name")
        parser.add_argument("--redirect-uri", action="append", default=[])
        parser.add_argument("--organization-id", action="append", type=int, default=[])
        parser.add_argument("--actor-id", type=int)
        parser.add_argument("--grant-id", type=int)
        parser.add_argument("--reason")
        parser.add_argument("--confirm")

    def _require(self, options, *keys):
        missing = [key for key in keys if not options.get(key)]
        if missing:
            raise CommandError("Не заданы обязательные параметры: " + ", ".join(missing))

    def _finance_actor_for(self, actor_id, organizations):
        user_model = get_user_model()
        actor = user_model.objects.filter(pk=actor_id, is_active=True).first()
        if actor is None:
            raise CommandError("--actor-id не относится к активному пользователю.")
        if not organizations or not all(
            can_manage_finance(actor, organization) for organization in organizations
        ):
            raise CommandError(
                "--actor-id не обладает правом управления финансами для всех указанных организаций."
            )
        return actor

    def _provision(self, options):
        self._require(
            options,
            "client_id",
            "client_name",
            "principal_subject",
            "principal_name",
            "redirect_uri",
            "organization_id",
            "actor_id",
            "confirm",
        )
        client_id = options["client_id"]
        if options["confirm"] != client_id:
            raise CommandError("Для provision укажите --confirm с точным значением --client-id.")
        if client_id != CHATGPT_CLIENT_ID_METADATA_URL:
            raise CommandError(
                "Для финансового MCP допускается только доверенный ChatGPT CIMD client_id."
            )
        redirect_uris = list(dict.fromkeys(options["redirect_uri"]))
        if (
            len(redirect_uris) != 1
            or not all(_exact_https_redirect_uri(uri) for uri in redirect_uris)
        ):
            raise CommandError("Укажите ровно один точный абсолютный HTTPS --redirect-uri без fragment.")
        organization_ids = list(dict.fromkeys(options["organization_id"]))
        if len(organization_ids) > MAX_ORGANIZATIONS:
            raise CommandError(
                f"Один финансовый MCP principal ограничен {MAX_ORGANIZATIONS} организациями."
            )
        organizations = list(Organization.objects.filter(pk__in=organization_ids).order_by("id"))
        if len(organizations) != len(organization_ids):
            raise CommandError("Одна или несколько --organization-id не существуют.")
        actor = self._finance_actor_for(options["actor_id"], organizations)
        # Fetch only the literal trusted document once during provisioning.
        # Runtime OAuth validates the pinned evidence below and never makes a
        # network request based on a client_id supplied by a caller.
        try:
            metadata = fetch_trusted_chatgpt_client_metadata()
        except FinanceMcpConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        if metadata["client_id"] != client_id or not set(redirect_uris).issubset(metadata["redirect_uris"]):
            raise CommandError(
                "Каждый --redirect-uri должен в точности присутствовать в доверенном ChatGPT CIMD."
            )
        with transaction.atomic():
            if FinanceMcpClient.objects.filter(client_id=client_id).exists():
                raise CommandError("Такой client_id уже существует; не изменяйте scope/redirect URI неявно.")
            principal, created = FinanceMcpPrincipal.objects.get_or_create(
                subject=options["principal_subject"],
                defaults={"display_name": options["principal_name"]},
            )
            if not created:
                # A scope belongs to the principal, therefore reusing one here
                # could silently widen an existing client's authorization.
                # Require an explicit new subject per public ChatGPT client.
                raise CommandError("Principal subject уже существует; создайте отдельный subject для нового OAuth client.")
            FinanceMcpClient.objects.create(
                client_id=client_id,
                display_name=options["client_name"],
                principal=principal,
                redirect_uris=redirect_uris,
                client_metadata_sha256=metadata["sha256"],
                client_metadata_verified_at=timezone.now(),
            )
            for organization in organizations:
                FinanceMcpPrincipalOrganization.objects.get_or_create(
                    principal=principal,
                    organization=organization,
                    defaults={"granted_by": actor},
                )
        self.stdout.write(self.style.SUCCESS(
            "Создан pre-registered public OAuth client для read-only financial MCP. "
            "client_id не является секретом; access/refresh tokens не созданы до owner approval."
        ))

    def _revoke(self, options):
        self._require(options, "reason", "confirm", "actor_id")
        client_id = options.get("client_id")
        grant_id = options.get("grant_id")
        if bool(client_id) == bool(grant_id):
            raise CommandError("Укажите ровно один из --client-id или --grant-id.")
        target = str(client_id or grant_id)
        if options["confirm"] != target:
            raise CommandError("Для revoke укажите --confirm с точным ID выбранной цели.")
        if client_id:
            client = FinanceMcpClient.objects.select_related("principal").filter(
                client_id=client_id
            ).first()
            if client is None:
                raise CommandError("OAuth client не найден.")
            organizations = list(
                client.principal.organization_scopes.values_list("organization_id", flat=True)
            )
            organizations = list(Organization.objects.filter(pk__in=organizations))
            self._finance_actor_for(options["actor_id"], organizations)
            revoke_client(client, reason=options["reason"])
        else:
            grant = FinanceMcpGrant.objects.select_related("principal").filter(pk=grant_id).first()
            if grant is None:
                raise CommandError("OAuth grant не найден.")
            organizations = list(
                grant.principal.organization_scopes.values_list("organization_id", flat=True)
            )
            self._finance_actor_for(
                options["actor_id"],
                list(Organization.objects.filter(pk__in=organizations)),
            )
            revoke_grant(grant, reason=options["reason"])
        self.stdout.write(self.style.SUCCESS(
            "Доступ отозван: активные access/refresh tokens выбранной цели больше не принимаются."
        ))

    def handle(self, *args, **options):
        if options["action"] == "provision":
            self._provision(options)
        else:
            self._revoke(options)
