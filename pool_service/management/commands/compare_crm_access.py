"""Private operator read for two known members; no grants, writes or CRM content."""
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from pool_service.communication_models import CommunicationAccess
from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.services.crm_commercial_access import can_view_team_deals
from pool_service.services.crm_lead_access import can_view_leads
from pool_service.services.staff_lifecycle import manages_staff

UNAVAILABLE = 'crm_access_comparison_unavailable'
CRM_FIELDS = (
    'view_team_deals', 'view_leads', 'create_own_leads', 'take_leads',
    'qualify_own_leads', 'edit_own_leads', 'transition_own_leads',
    'edit_own_deals', 'transition_own_deals', 'create_own_deals',
    'manage_own_next_action',
)
COMM_FIELDS = ('can_view_conversations', 'can_view_all_calls', 'can_view_own_calls')


def _scope(actor_id, organization_id, user_ids):
    org = Organization.objects.filter(pk=organization_id).first()
    actor = get_user_model().objects.filter(pk=actor_id).first()
    if not org or not manages_staff(actor, org):
        raise CommandError(UNAVAILABLE)
    members = set(OrganizationAccess.objects.filter(organization=org,
        user_id__in=user_ids).values_list('user_id', flat=True))
    if members != set(user_ids):
        raise CommandError(UNAVAILABLE)
    return org


class Command(BaseCommand):
    requires_system_checks = []
    help = 'Private operator only: compare two known organization members without writes.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--actor', type=int, required=True)
        parser.add_argument('--left-user', type=int, required=True)
        parser.add_argument('--right-user', type=int, required=True)

    def handle(self, *args, **options):
        try:
            actor_id, org_id, left, right = (options[key] for key in
                ('actor', 'organization', 'left_user', 'right_user'))
            if any(type(value) is not int or not 0 < value < 2**63
                   for value in (actor_id, org_id, left, right)) or left == right:
                raise CommandError(UNAVAILABLE)
            org = _scope(actor_id, org_id, (left, right))
            # Explicit IDs only; no usernames, email, phones, card counts or contents.
            rows = []
            for user_id in (left, right):
                user = get_user_model().objects.get(pk=user_id)
                grant = CrmAccess.objects.filter(organization=org, user=user).first()
                comm = CommunicationAccess.objects.filter(organization=org, user=user).first()
                crm_flags = {field: bool(grant and getattr(grant, field)) for field in CRM_FIELDS}
                rows.append({
                    'user_id': user_id,
                    'active': user.is_active,
                    'roles': sorted(set(OrganizationAccess.objects.filter(organization=org,
                        user=user).values_list('role', flat=True))),
                    'employee_blocks_access': Employee.objects.filter(organization=org, user=user).filter(
                        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists(),
                    'crm_grant_present': grant is not None,
                    'crm_flags': crm_flags,
                    'communication_grant_present': comm is not None,
                    'communication_raw_flags': {field: bool(comm and getattr(comm, field)) for field in COMM_FIELDS},
                    'can_enter_leads': can_view_leads(user, org),
                    'can_enter_deals': can_view_team_deals(user, org),
                    # Not a full create-card authorization: object/pipeline predicates still apply.
                    'own_deal_create_flags_enabled': bool(org.crm_v2_write and
                        crm_flags['create_own_deals'] and crm_flags['manage_own_next_action']),
                })
            result = {'schema_version': 1, 'organization_id': org.pk,
                'crm_v2_read': org.crm_v2_read, 'crm_v2_write': org.crm_v2_write,
                'users': rows, 'different_crm_flags': [field for field in CRM_FIELDS
                    if rows[0]['crm_flags'][field] != rows[1]['crm_flags'][field]],
                'limitations': ['membership_snapshot_not_atomic', 'card_and_call_visibility_not_tested',
                    'communication_flags_are_raw_not_effective', 'no_permission_changes']}
            _scope(actor_id, org_id, (left, right))
        except Exception:
            # Fail closed, with the same error for missing, foreign, denied and invalid inputs.
            raise CommandError(UNAVAILABLE) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
