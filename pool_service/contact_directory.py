"""Server-paged Contacts directory; no writes or external source reads."""
import re
import unicodedata
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Value, Window
from django.db.models.functions import Replace, RowNumber, Trim
from .client_queries import readable_clients, readable_company_links
from .client_crm_models import ClientBusinessRole, ClientContact
from .models import ClientAccess, PoolAccess
from .phone_utils import format_phone, normalize_phone

PAGE_SIZE = 50
RELATED_LIMIT = 20


def _tokens(query):
    text = unicodedata.normalize("NFKC", query).casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def _phone_expression(field):
    expression = F(field)
    for character in ("+", " ", "-", "(", ")", ".", "\u00a0"):
        expression = Replace(expression, Value(character), Value(""))
    return expression


def _clause(fields, token):
    pattern = re.escape(token).replace("е", "[её]")
    clause = Q()
    for field in fields:
        clause |= Q(**{field + "__iregex": pattern})
    return clause


def search_contacts(queryset, user, query):
    tokens = _tokens(query)
    if len(query) > 160 or len(tokens) > 12:
        return queryset.none()
    if not tokens:
        return queryset if not query.strip() else queryset.none()
    links = readable_company_links(user)
    fields = ("name", "company_name", "first_name", "last_name", "contact_position",
              "inn", "email", "phone", "crm_profile__legal_name", "crm_profile__middle_name")
    predicate = Q()
    # Subqueries keep duplicate emails/relations from multiplying paginated rows.
    for token in tokens:
        direct = _clause(fields, token)
        contacts = ClientContact.objects.filter(_clause(("value", "match_value"), token)).values("client_id")
        people = links.filter(_clause(("person__name", "person__phone", "person__email", "position"), token)).values("company_id")
        companies = links.filter(_clause(("company__name", "company__inn", "company__phone", "company__email", "position"), token)).values("person_id")
        clause = direct | Q(pk__in=contacts) | Q(pk__in=people) | Q(pk__in=companies)
        if token.isdigit() and len(token) >= 4:
            own_phone = readable_clients(user).annotate(directory_phone=_phone_expression("phone")).filter(directory_phone__contains=token).values("pk")
            linked_people = links.annotate(directory_phone=_phone_expression("person__phone")).filter(directory_phone__contains=token).values("company_id")
            linked_companies = links.annotate(directory_phone=_phone_expression("company__phone")).filter(directory_phone__contains=token).values("person_id")
            clause |= Q(pk__in=own_phone) | Q(pk__in=linked_people) | Q(pk__in=linked_companies)
        predicate &= clause
    national = normalize_phone(query) if not any(c.isalpha() for c in query) else ""
    if len(national) == 10 and national.isdigit():
        def aliases(rows, field):
            # Explicit +8 is international, unlike a bare Russian trunk 8.
            rows = rows.annotate(directory_phone=_phone_expression(field), directory_raw=Trim(F(field)))
            return rows.filter(Q(directory_phone__in=[national, "7" + national])
                | (Q(directory_phone="8" + national) & ~Q(directory_raw__startswith="+")))
        phone_predicate = Q(pk__in=aliases(readable_clients(user), "phone").values("pk"))
        phone_predicate |= Q(pk__in=aliases(links, "person__phone").values("company_id"))
        phone_predicate |= Q(pk__in=aliases(links, "company__phone").values("person_id"))
        contacts = ClientContact.objects.filter(kind=ClientContact.KIND_PHONE)
        phone_predicate |= Q(pk__in=aliases(contacts, "value").values("client_id"))
        phone_predicate |= Q(pk__in=contacts.filter(match_value=national).values("client_id"))
        digits = "".join(c for c in query if c.isdigit())
        if len(digits) == 11:
            # A full Russian phone query is an identity-shaped search: loose
            # token matching must not also pull in an explicit +8 foreign value.
            return queryset.filter(phone_predicate | Q(inn=digits))
        predicate |= phone_predicate
    return queryset.filter(predicate)


def page(queryset, request, group):
    paginator = Paginator(queryset, PAGE_SIZE)
    result = paginator.get_page(request.GET.get(group + "_page", "1"))
    def url(number):
        params = request.GET.copy()
        params[group + "_page"] = str(number)
        params["clients_tab"] = "people" if group == "staff" else group
        return "?" + params.urlencode()
    result.previous_url = url(result.previous_page_number()) if result.has_previous() else None
    result.next_url = url(result.next_page_number()) if result.has_next() else None
    return result


def _bounded(queryset, partition):
    return queryset.annotate(directory_row=Window(expression=RowNumber(), partition_by=[F(partition)],
        order_by=[F("is_primary").desc(), F("pk").asc()])).filter(directory_row__lte=RELATED_LIMIT + 1).order_by(partition, "directory_row")


def directory_context(request):
    allowed = readable_clients(request.user)
    supplier_ids = ClientBusinessRole.objects.filter(role="supplier", active=True).values("client_id")
    groups = {"companies": allowed.filter(client_type="legal"), "people": allowed.filter(client_type="private"),
              "suppliers": allowed.filter(pk__in=supplier_ids)}
    pages = {}
    for group, queryset in groups.items():
        query = request.GET.get(group + "_q", "")
        queryset = search_contacts(queryset, request.user, query)
        pages[group] = page(queryset.annotate(pool_count=Count("pool")).select_related("organization", "crm_profile").order_by("name", "pk"), request, group)
    clients = {client.pk: client for result in pages.values() for client in result.object_list}
    links = readable_company_links(request.user)
    people_by_company, companies_by_person = {}, {}
    for link in _bounded(links.filter(company_id__in=clients), "company_id").select_related("person"):
        people_by_company.setdefault(link.company_id, []).append(link)
    for link in _bounded(links.filter(person_id__in=clients), "person_id").select_related("company"):
        companies_by_person.setdefault(link.person_id, []).append(link)
    staff_by_company = {}
    for access in ClientAccess.objects.filter(client_id__in=clients).annotate(
        directory_row=Window(expression=RowNumber(), partition_by=[F("client_id")], order_by=F("pk").asc())
    ).filter(directory_row__lte=RELATED_LIMIT + 1).select_related("user"):
        staff_by_company.setdefault(access.client_id, []).append(access)
    for item in clients.values():
        item.phone_display = format_phone(item.phone)
        if item.client_type == "legal":
            item.primary_contact = {"name": " ".join(part for part in [item.first_name, item.last_name] if part),
                "position": item.contact_position, "phone": item.phone_display, "email": item.email}
            if not any(item.primary_contact.values()): item.primary_contact = None
            related = people_by_company.get(item.pk, [])
            item.linked_people = related[:RELATED_LIMIT]
            item.more_relationships = len(related) > RELATED_LIMIT
            staff = staff_by_company.get(item.pk, [])
            item.staff_contacts = staff[:RELATED_LIMIT]
            item.more_staff_contacts = len(staff) > RELATED_LIMIT
            for link in item.linked_people: link.person.phone_display = format_phone(link.person.phone)
        else:
            related = companies_by_person.get(item.pk, [])
            item.linked_companies = related[:RELATED_LIMIT]
            item.more_relationships = len(related) > RELATED_LIMIT
            for link in item.linked_companies: link.company.phone_display = format_phone(link.company.phone)
    # Each Page can contain a separate ORM instance of a dual-role Client.
    for result in pages.values():
        result.object_list = [clients[item.pk] for item in result.object_list]
    staff = PoolAccess.objects.filter(pool__client_id__in=allowed.values("pk"),
        pool__organization_id=F("pool__client__organization_id"))
    staff_query = request.GET.get("people_q", "")
    tokens = _tokens(staff_query)
    if len(staff_query) > 160 or len(tokens) > 12 or (staff_query.strip() and not tokens):
        staff = staff.none()
    for token in tokens[:12]:
        staff = staff.filter(_clause(("user__first_name", "user__last_name", "user__username", "user__email", "pool__client__name", "pool__address"), token))
    staff_page = page(staff.select_related("pool__client", "pool__organization", "user").order_by("pool__client__name", "pk"), request, "staff")
    return {"companies": pages["companies"].object_list, "private_contacts": pages["people"].object_list,
        "suppliers": pages["suppliers"].object_list, "pool_staff": staff_page.object_list,
        "companies_page": pages["companies"], "people_page": pages["people"], "suppliers_page": pages["suppliers"], "staff_page": staff_page,
        "companies_q": request.GET.get("companies_q", "")[:160], "people_q": staff_query[:160], "suppliers_q": request.GET.get("suppliers_q", "")[:160]}
