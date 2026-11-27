from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .client_queries import readable_clients
from .communication_secrets import encrypt_secret
from .communication_services import selected_communication_access
from .mailbox_forms import ComposeForm, MailboxSetupForm, NewMailLeadForm
from .mailbox_models import CrmMailbox, MailLetter
from .models import Organization
from .services import mailbox_imap as provider
from .services.crm_role_runtime import resolve_crm_capabilities
from .services.mailbox_access import mailbox_role
from .services.mailbox_mime import parse_content
from .services.mailbox_routing import bind_mail, contact_letters, visible_link, writable_cards
from .services.mailbox_sending import deliver
from .services.mailbox_sync import PROVIDER_ERRORS, sync_mailbox


def _scope(request, *, owner=False):
    membership = selected_communication_access(request)
    if not membership:
        raise PermissionDenied
    org = membership.organization
    role = mailbox_role(request.user, org)
    if not role or (owner and role != "owner"):
        raise PermissionDenied
    return org, role


def _url(name, org, *args, **params):
    return reverse(name, args=args) + "?" + urlencode({"org_id": org.pk, **params})


def _page(request, org, section, **context):
    response = render(
        request,
        "pool_service/mailbox/" + section + ".html",
        {"org": org, "communication_org_id": org.pk, "active_tab": "communications", **context},
    )
    response["Referrer-Policy"] = "same-origin"
    return response


def _letter(org, pk):
    return get_object_or_404(
        MailLetter.objects.select_related("mailbox", "mailbox__organization"),
        pk=pk,
        mailbox__organization=org,
    )


@login_required
@never_cache
@require_GET
def inbox(request):
    org, role = _scope(request)
    box = CrmMailbox.objects.filter(organization=org).first()
    folder_id = request.GET.get("folder", "all")
    query = request.GET.get("q", "").strip()[:160]
    client = None
    letters = MailLetter.objects.none()
    folders = []
    if box:
        folders = box.folders.filter(available=True).order_by("name")
        letters = box.letters.filter(
            Q(commercial=True) | Q(locations__folder__available=True) | Q(link_kind="manual")
        )
        if request.GET.get("client"):
            client = get_object_or_404(
                readable_clients(request.user), organization=org, pk=_number(request.GET["client"])
            )
            letters = contact_letters(box, client)
        if folder_id == "review":
            letters = letters.filter(needs_attention=True)
        elif folder_id == "sent":
            letters = letters.filter(outgoing=True, commercial=True)
        elif folder_id != "all":
            folder = get_object_or_404(folders, pk=_number(folder_id))
            letters = letters.filter(locations__folder=folder)
        if query:
            letters = letters.filter(
                Q(subject__icontains=query)
                | Q(sender__icontains=query)
                | Q(sender_name__icontains=query)
                | Q(peers__address__icontains=query)
                | Q(body__icontains=query)
            ).distinct()
    page = Paginator(
        letters.defer("body", "attachments").order_by("-sent_at", "-pk").distinct(), 40
    ).get_page(request.GET.get("page"))
    filters = {"folder": folder_id, "q": query}
    if client:
        filters["client"] = client.pk
    return _page(
        request,
        org,
        "inbox",
        box=box,
        owner=role == "owner",
        folders=folders,
        chosen=folder_id,
        query=query,
        client=client,
        page=page,
        paging=_url("crm_mail", org, **filters),
        review_count=box.letters.filter(needs_attention=True).count() if box else 0,
    )


def _number(value):
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 18:
        raise Http404
    return int(value)


@login_required
@never_cache
@sensitive_post_parameters("password")
@sensitive_variables("password", "cipher")
@require_http_methods(["GET", "POST"])
def setup(request):
    org, _ = _scope(request, owner=True)
    box = CrmMailbox.objects.filter(organization=org).first()
    initial = {"address": box.address if box else request.GET.get("address", "")[:254]}
    form = MailboxSetupForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and request.POST.get("action") == "pause" and box:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=org.pk)
            _scope(request, owner=True)
            CrmMailbox.objects.filter(pk=box.pk).update(
                enabled=False, credential="", sync_token=None, lease_until=None
            )
        return redirect(_url("crm_mail", org))
    if request.method == "POST" and form.is_valid():
        address = form.cleaned_data["address"].casefold()
        if box and box.address != address:
            form.add_error("address", "Для этого архива укажите ранее подключённый адрес.")
        else:
            try:
                password = form.cleaned_data["password"]
                with provider.connect(address, password) as session:
                    provider.folders(session)
                cipher = encrypt_secret(password)
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=org.pk)
                    _scope(request, owner=True)
                    current = CrmMailbox.objects.select_for_update().filter(organization=org).first()
                    if current and current.address != address:
                        raise ValidationError("Для организации уже подключён другой ящик.")
                    CrmMailbox.objects.update_or_create(
                        organization=org,
                        address=address,
                        defaults={
                            "credential": cipher,
                            "enabled": True,
                            "sync_error": "",
                            "sync_token": None,
                            "lease_until": None,
                        },
                    )
                messages.success(
                    request, "Почтовый ящик подключён. Синхронизация папок начнётся в ближайшую минуту."
                )
                return redirect(_url("crm_mail", org))
            except PROVIDER_ERRORS:
                form.add_error(
                    None, "VK WorkSpace не подтвердил подключение. Проверьте адрес и пароль приложения."
                )
            except (IntegrityError, ValidationError):
                form.add_error(None, "Не удалось сохранить подключение под этим адресом.")
    return _page(request, org, "setup", form=form, box=box)


@login_required
@never_cache
@require_POST
def refresh(request):
    org, _ = _scope(request)
    box = get_object_or_404(CrmMailbox, organization=org, enabled=True)
    sync_mailbox(box.pk, seconds=10)
    return redirect(_url("crm_mail", org))


@login_required
@never_cache
@require_GET
def detail(request, pk):
    org, _ = _scope(request)
    letter = _letter(org, pk)
    error = ""
    if not letter.body_loaded and letter.mailbox.enabled:
        try:
            letter.body, letter.attachments = parse_content(provider.raw_message(letter))
            letter.body_loaded = True
            letter.save(update_fields=["body", "attachments", "body_loaded"])
        except (ValueError, LookupError, UnicodeError) + PROVIDER_ERRORS:
            error = "Текст пока недоступен. Обновите письмо или откройте его в VK WorkSpace."
    _scope(request)
    return _page(
        request, org, "detail", letter=letter, error=error, link=visible_link(letter, request.user, org)
    )


@login_required
@never_cache
@require_GET
def attachment(request, pk, index):
    org, _ = _scope(request)
    letter = _letter(org, pk)
    if not any(item["index"] == index for item in letter.attachments):
        raise Http404
    try:
        receipt = (
            letter.mailsubmission_set.filter(mailbox=letter.mailbox, status__in=("sent", "partial"))
            .exclude(attachment_source="")
            .first()
        )
        if receipt:
            with receipt.attachment_source.open("rb") as stored:
                raw = stored.read()
        elif letter.mailbox.enabled:
            raw = provider.raw_message(letter)
        else:
            raise Http404
        name, data = parse_content(raw, download_index=index)
    except (ValueError,) + PROVIDER_ERRORS:
        raise Http404 from None
    _scope(request)
    response = HttpResponse(data, content_type="application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(True, name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@login_required
@never_cache
@require_POST
def processed(request, pk):
    org, _ = _scope(request)
    get_object_or_404(MailLetter, pk=pk, mailbox__organization=org)
    MailLetter.objects.filter(pk=pk, mailbox__organization=org).update(
        needs_attention=False, processed_at=timezone.now()
    )
    return redirect(_url("crm_mail_detail", org, pk))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def link(request, pk):
    org, _ = _scope(request)
    letter = _letter(org, pk)
    term = request.GET.get("q", "").strip()[:120]
    form = NewMailLeadForm(
        request.POST if request.method == "POST" and request.POST.get("kind") == "new_lead" else None,
        initial={"title": letter.subject[:255] or "Обращение по почте"},
    )
    if request.method == "POST":
        kind = request.POST.get("kind")
        try:
            revision = _number(request.POST.get("revision"))
            if kind == "new_lead":
                if form.is_valid():
                    data = form.cleaned_data
                    bind_mail(
                        request.user,
                        org,
                        pk,
                        revision,
                        kind,
                        new_lead={
                            "title": data["title"],
                            "need": data["need"],
                            "next_action": {"title": data["next_title"], "due_at": data["due"]},
                        },
                    )
                    return redirect(_url("crm_mail_detail", org, pk))
            else:
                bind_mail(request.user, org, pk, revision, kind, target=_number(request.POST.get("target")))
                return redirect(_url("crm_mail_detail", org, pk))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            letter.refresh_from_db()
    options = {}
    for kind in ("lead", "deal"):
        cards = writable_cards(kind, request.user, org)
        if term:
            match = Q(title__icontains=term)
            numeric = term.lstrip("#")
            if numeric.isascii() and numeric.isdigit() and len(numeric) <= 18:
                match |= Q(pk=int(numeric))
            cards = cards.filter(match)
        options[kind] = cards.order_by("-pk").values("pk", "title")[:35]
    grant = resolve_crm_capabilities(request.user.pk, org.pk)
    return _page(
        request,
        org,
        "link",
        letter=letter,
        leads=options["lead"],
        deals=options["deal"],
        can_create=org.crm_v2_write and grant.view_leads and grant.create_own_leads,
        form=form,
        term=term,
        current=visible_link(letter, request.user, org),
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def compose(request, pk=None):
    org, _ = _scope(request)
    box = get_object_or_404(CrmMailbox, organization=org, enabled=True)
    parent = _letter(org, pk) if pk else None
    scope = [request.user.pk, box.pk, pk]
    initial = {"token": signing.dumps([*scope, str(uuid4())], salt="mail-send")}
    if parent:
        initial["to"] = ", ".join(parent.recipients) if parent.outgoing else parent.reply_to or parent.sender
        initial["subject"] = (
            parent.subject if parent.subject.lower().startswith("re:") else "Re: " + parent.subject
        )[:250]
    form = ComposeForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            saved = signing.loads(form.cleaned_data["token"], salt="mail-send", max_age=86400)
            if saved[:3] != scope:
                raise signing.BadSignature()
            receipt = deliver(
                box, request.user, UUID(saved[3]), form.cleaned_data, request.FILES.getlist("files"), parent
            )
        except signing.BadSignature:
            form.add_error(None, "Срок действия формы истёк. Откройте новое письмо.")
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if receipt.status in ("sent", "partial") and receipt.letter_id:
                if receipt.status == "partial":
                    messages.warning(request, "Почтовый сервер принял письмо только для части получателей.")
                return redirect(_url("crm_mail_detail", org, receipt.letter_id))
            form.add_error(
                None, "Отправку подтвердить не удалось. Проверьте папку «Отправленные» перед новой попыткой."
            )
    return _page(request, org, "compose", parent=parent, form=form, box=box)
