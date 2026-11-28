import re
import smtplib
from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Substr
from django.http import Http404, HttpResponse, JsonResponse
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
from .mailbox_forms import ComposeForm, MailboxSetupForm, MailSignatureForm, NewMailLeadForm
from .mailbox_models import CrmMailbox, MailFolder, MailLetter, MailLocation, MailReadReceipt, MailSignature
from .models import Employee, Organization
from .services import mailbox_imap as provider
from .services.crm_role_runtime import resolve_crm_capabilities
from .services.mailbox_access import mailbox_role
from .services.mailbox_mime import CONTENT_VERSION, body_preview, cache_content, digest, parse_content
from .services.mailbox_routing import bind_mail, contact_letters, visible_link, writable_cards
from .services.mailbox_sending import deliver
from .services.mailbox_transport import smtp_session
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
    modal = request.headers.get("X-Mail-Dialog") == "1" and section in ("compose", "sender", "signature")
    response = render(
        request,
        "pool_service/mailbox/" + ("_" if modal else "") + section + ".html",
        {"org": org, "communication_org_id": org.pk, "active_tab": "communications", **context},
    )
    if modal:
        response["X-Mail-Dialog"] = "1"
    response["Referrer-Policy"] = "same-origin"
    return response


def _letter(org, pk):
    return get_object_or_404(
        MailLetter.objects.select_related("mailbox", "mailbox__organization"),
        pk=pk,
        mailbox__organization=org,
    )


def _mail_avatar(letter):
    address = letter.sender
    name = letter.sender_name
    if letter.outgoing:
        address = next(iter(letter.recipients), "")
        name = ""
    local, _, domain = address.casefold().rpartition("@")
    brand = domain == "avito.ru" or domain.endswith(".avito.ru")
    words = re.findall(r"[^\W_]+", name if name and "@" not in name else local)
    initials = "".join(word[0] for word in words[:2]).upper() or "✉"
    color = int(digest(address.casefold() or name)[:2], 16) % 6
    return brand, initials[:2], color


@login_required
@never_cache
@require_GET
def inbox(request):
    org, role = _scope(request)
    boxes = list(CrmMailbox.objects.filter(organization=org).defer("credential").order_by("address"))
    box_id = request.GET.get("box")
    box = None
    if box_id:
        pk = _number(box_id)
        box = next((item for item in boxes if item.pk == pk), None)
        if box is None:
            raise Http404
    active_boxes = [item for item in boxes if item.enabled]
    folder_id = request.GET.get("folder", "all")
    query = request.GET.get("q", "").strip()[:160]
    client = None
    letters = MailLetter.objects.filter(mailbox__organization=org)
    folders = MailFolder.objects.filter(mailbox__organization=org, available=True)
    if box:
        letters = letters.filter(mailbox=box)
        folders = folders.filter(mailbox=box)
    review_count = letters.filter(needs_attention=True).count()
    if request.GET.get("client"):
        client = get_object_or_404(
            readable_clients(request.user), organization=org, pk=_number(request.GET["client"])
        )
        letters = contact_letters(org, client, letters=letters)
    else:
        letters = letters.filter(Q(commercial=True) | Q(locations__folder__available=True) | Q(link_kind="manual"))
    if folder_id == "review":
        letters = letters.filter(needs_attention=True)
    elif folder_id == "sent":
        letters = letters.filter(outgoing=True, commercial=True)
    elif folder_id != "all":
        folder = get_object_or_404(folders, pk=_number(folder_id))
        letters = letters.filter(locations__folder=folder)
    if query:
        letters = letters.filter(Q(subject__icontains=query) | Q(sender__icontains=query)
                                 | Q(sender_name__icontains=query) | Q(peers__address__icontains=query)
                                 | Q(body__icontains=query) | Q(preview__icontains=query))
    location = MailLocation.objects.filter(
        letter=OuterRef("pk"), folder__mailbox_id=OuterRef("mailbox_id"), folder__available=True,
        generation=F("folder__generation"),
    ).order_by("pk")
    if folder_id not in ("all", "review", "sent"):
        location = location.filter(folder_id=folder.pk)
    letters = letters.select_related("mailbox").defer("body", "mailbox__credential").annotate(
        seen_here=Exists(MailReadReceipt.objects.filter(letter=OuterRef("pk"), user=request.user)),
        cached_start=Substr("body", 1, 1500),
        preview_folder=Subquery(location.values("folder_id")[:1]),
    ).order_by("-sent_at", "-pk").distinct()
    page = Paginator(letters, 40).get_page(request.GET.get("page"))
    today = timezone.localdate()
    for letter in page.object_list:
        age = (today - timezone.localdate(letter.sent_at)).days
        letter.show_date = letter.outgoing or age != 0
        letter.avito_avatar, letter.initials, letter.avatar_color = _mail_avatar(letter)
        letter.shows_files = bool(letter.attachments) if letter.body_loaded else letter.has_attachments
        letter.date_group = next(label for days, label in (
            (0, "Сегодня"), (1, "Вчера"), (6, "Неделя"), (29, "Месяц"), (float("inf"), "Ранее")
        ) if age <= days)
        letter.snippet = letter.preview if letter.preview is not None else body_preview(letter.cached_start)
        letter.unread = not (letter.outgoing or letter.seen_here)
        letter.needs_metadata = (letter.preview is None and not letter.snippet) or letter.shows_files is None
    filters = {"folder": folder_id, "q": query}
    if box:
        filters["box"] = box.pk
    if client:
        filters["client"] = client.pk
    return _page(request, org, "inbox", boxes=boxes, box=box, active_boxes=active_boxes, owner=role == "owner",
                 folders=folders.select_related("mailbox").defer("mailbox__credential").order_by("mailbox__address", "name"),
                 chosen=folder_id, query=query, client=client, page=page, review_count=review_count,
                 status_box=box or (boxes[0] if len(boxes) == 1 else None),
                 paging=_url("crm_mail", org, **filters),
                 read_query=urlencode({"org_id": org.pk, **filters, "page": page.number}))


def _number(value):
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 18:
        raise Http404
    return int(value)


@login_required
@never_cache
@require_POST
def previews(request, folder_id):
    org, _ = _scope(request)
    folder = get_object_or_404(MailFolder.objects.select_related("mailbox"), pk=folder_id,
                               mailbox__organization=org, available=True)
    ids = request.POST.getlist("letter")
    if not ids or len(ids) > 40:
        raise Http404
    ids = {_number(value) for value in ids}
    locations = MailLocation.objects.filter(
        folder=folder, generation=folder.generation, letter_id__in=ids, letter__mailbox=folder.mailbox,
    )
    missing_previews = dict(locations.filter(letter__preview__isnull=True).values_list("uid", "size"))
    missing_flags = set(locations.filter(letter__body_loaded=False, letter__has_attachments__isnull=True)
                        .values_list("uid", flat=True))
    if (missing_previews or missing_flags) and folder.mailbox.enabled:
        try:
            with provider.mailbox_session(folder.mailbox) as client:
                generation, _ = provider.select(client, folder.wire_name)
                if generation != folder.generation:
                    raise provider.MailUnavailable()
                snippets = provider.selected_previews(client, missing_previews)
                try:
                    flags = provider.selected_attachment_flags(client, missing_flags)
                except PROVIDER_ERRORS:
                    flags = {}  # A provider can decline BODYSTRUCTURE while previews still work.
            with transaction.atomic():
                box = CrmMailbox.objects.select_for_update().get(pk=folder.mailbox_id)
                _scope(request)
                if box.enabled and box.config_version == folder.mailbox.config_version:
                    current = locations.filter(uid__in=snippets.keys() | flags.keys(),
                                               folder__generation=generation, folder__available=True)
                    updates, indicators = {}, {}
                    for uid, letter_id in current.values_list("uid", "letter_id"):
                        if uid in snippets and not updates.get(letter_id):
                            updates[letter_id] = snippets[uid]
                        if uid in flags:
                            indicators[letter_id] = indicators.get(letter_id, False) or flags[uid]
                    box.letters.filter(preview__isnull=True).bulk_update(
                        [MailLetter(pk=pk, preview=text) for pk, text in updates.items()], ["preview"]
                    )
                    box.letters.filter(body_loaded=False, has_attachments__isnull=True).bulk_update(
                        [MailLetter(pk=pk, has_attachments=value) for pk, value in indicators.items()],
                        ["has_attachments"],
                    )
        except (ValueError, LookupError, UnicodeError) + PROVIDER_ERRORS:
            pass  # Keep the list usable if a provider is temporarily unavailable.
    _scope(request)
    current = locations.filter(folder__available=True, folder__generation=folder.generation,
                               folder__mailbox__config_version=folder.mailbox.config_version)
    rows = current.values_list("letter_id", "letter__preview", "letter__has_attachments")
    snippets, flags = {}, {}
    for pk, snippet, flag in rows:
        if snippet is not None:
            snippets[pk] = snippet
        if flag is not None:
            flags[pk] = flag
    return JsonResponse({"previews": snippets, "attachments": flags})


def _signature_text(user, org):
    stored = MailSignature.objects.filter(organization=org, user=user).values_list("text", flat=True).first()
    if stored is not None:
        return stored
    name = user.get_full_name().strip() or Employee.objects.filter(
        user=user, organization=org, is_active=True
    ).order_by("pk").values_list("display_name", flat=True).first()
    return "\n".join(filter(None, ("С уважением,", name, org.name)))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def signature(request):
    org, _ = _scope(request)
    form = MailSignatureForm(request.POST if request.method == "POST" else None,
                             initial={"text": _signature_text(request.user, org)})
    if request.method == "POST" and form.is_valid():
        content = form.cleaned_data["text"].replace("\r\n", "\n")
        MailSignature.objects.update_or_create(organization=org, user=request.user, defaults={"text": content})
        if request.headers.get("X-Mail-Dialog") == "1":
            return JsonResponse({"signature": content})
        return redirect(_url("crm_mail", org))
    return _page(request, org, "signature", form=form)


@transaction.atomic
@sensitive_variables("values")
def _save_account(request, org, box, candidate, version, values):
    Organization.objects.select_for_update().get(pk=org.pk)
    _scope(request, owner=True)
    if box is None:
        return CrmMailbox.objects.create(organization=org, **values)
    current = CrmMailbox.objects.select_for_update().get(organization=org, pk=box.pk)
    if current.config_version != version:
        raise ValidationError("Настройки изменились. Заново откройте ящик.")
    if current.incoming_account != candidate.incoming_account:
        # UIDs and UIDVALIDITY are local to one IMAP account. Keep the CRM letters and links.
        MailLocation.objects.filter(folder__mailbox=current).delete()
        current.folders.update(available=False, generation=0, latest_uid=0,
                               backfill_uid=0, audit_uid=0, scanned_at=None)
        values.update(connected_at=timezone.now(), last_sync_at=None, last_attempt_at=None)
    if any(getattr(current, name) != value for name, value in values.items()):
        values.update(enabled=True, sync_token=None, lease_until=None, config_version=current.config_version + 1)
        if "credential" in values:
            values["sync_error"] = ""
        CrmMailbox.objects.filter(pk=current.pk).update(**values)


@login_required
@never_cache
@sensitive_post_parameters("password")
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def setup(request, mailbox_id=None, *, legacy=False):
    org, _ = _scope(request, owner=True)
    settings_url = _url("communications_channels", org) + "#mailboxes"
    boxes = CrmMailbox.objects.filter(organization=org)
    initial = {}
    data = request.POST if request.method == "POST" else None
    if legacy:
        address = (request.POST.get("address") or request.GET.get("address", ""))[:254].strip().casefold()
        if address:
            mailbox_id = boxes.filter(address__iexact=address).values_list("pk", flat=True).first()
            initial["address"] = address
        else:
            ids = list(boxes.values_list("pk", flat=True)[:2])
            if len(ids) > 1:
                if data is not None:
                    raise Http404
                return redirect(settings_url)
            mailbox_id = ids[0] if ids else None
        if request.method == "GET":
            if mailbox_id:
                return redirect(_url("crm_mail_account", org, mailbox_id))
            return redirect(_url("crm_mail_add", org, **initial))
    box = get_object_or_404(boxes, pk=mailbox_id) if mailbox_id else None
    if not box:
        initial["address"] = request.GET.get("address", "")[:254]
    if legacy and data is not None:
        # Forms opened before deployment only contained address and password.
        data = data.copy()
        if box and box.config_version:
            messages.warning(request, "Настройки изменились. Откройте ящик и проверьте актуальные параметры.")
            return redirect(settings_url)
        data.setdefault("provider", "mailru")
        data.setdefault("version", "0")
    if data is not None and data.get("action") == "pause":
        if not box:
            raise Http404
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=org.pk)
            _scope(request, owner=True)
            if data.get("version") == str(box.config_version):
                changed = boxes.filter(pk=box.pk, config_version=box.config_version).update(
                    enabled=False, credential="", sync_token=None, lease_until=None, sync_error="",
                    config_version=box.config_version + 1,
                )
            else:
                changed = False
        if not changed:
            messages.warning(request, "Настройки уже изменились. Проверьте состояние ящика.")
        return redirect(settings_url)
    form = MailboxSetupForm(data, initial=initial, mailbox=box)
    if data is not None and form.is_valid():
        fields = form.cleaned_data
        names = ("address", "provider", "login", "imap_host", "imap_port", "smtp_host", "smtp_port")
        values = {name: fields[name] for name in names}
        candidate = CrmMailbox(organization=org, **values)
        password = fields["password"]
        if box and fields["version"] != box.config_version:
            form.add_error(None, "Настройки изменились. Заново откройте ящик.")
        elif not box and CrmMailbox.objects.filter(address__iexact=candidate.address).exists():
            form.add_error("address", "Не удалось добавить ящик с таким адресом.")
        else:
            phase = "IMAP"
            try:
                if password:
                    with provider.connect(candidate, password) as session:
                        provider.folders(session)
                    phase = "SMTP"
                    with smtp_session(candidate, password):
                        pass
                    values["credential"] = encrypt_secret(password)
                _save_account(request, org, box, candidate, fields["version"], values)
                messages.success(request, "Настройки почтового ящика сохранены.")
                return redirect(settings_url)
            except (smtplib.SMTPException, UnicodeError) + PROVIDER_ERRORS:
                form.add_error(None, f"Не удалось подключиться по {phase}. Проверьте сервер, логин и пароль приложения.")
            except IntegrityError:
                form.add_error("address", "Не удалось добавить ящик с таким адресом.")
            except ValidationError as exc:
                form.add_error(None, exc)
    return _page(request, org, "setup", form=form, box=box)


@login_required
@never_cache
@require_POST
def refresh(request):
    org, _ = _scope(request)
    boxes = CrmMailbox.objects.filter(organization=org, enabled=True)
    box_id = request.POST.get("box") or request.GET.get("box")
    if box_id:
        box = get_object_or_404(boxes, pk=_number(box_id))
    else:
        ids = list(boxes.values_list("pk", flat=True)[:2])
        if len(ids) != 1:
            messages.info(request, "Для ручного обновления выберите почтовый ящик.")
            return redirect(_url("crm_mail", org))
        box = boxes.get(pk=ids[0])
    sync_mailbox(box.pk, seconds=10)
    return redirect(_url("crm_mail", org, box=box.pk))


@login_required
@never_cache
@require_GET
def detail(request, pk):
    org, _ = _scope(request)
    letter = _letter(org, pk)
    error = ""
    cached_old = letter.body_loaded and letter.content_version < CONTENT_VERSION
    if letter.mailbox.enabled and (not letter.body_loaded or (cached_old and letter.locations.exists())):
        try:
            cache_content(letter, provider.raw_message(letter))
        except (ValueError, LookupError, UnicodeError) + PROVIDER_ERRORS:
            error = "Текст пока недоступен. Попробуйте обновить письмо позже."
    _scope(request)
    if letter.body_loaded and not letter.outgoing:
        MailReadReceipt.objects.get_or_create(letter=letter, user=request.user)
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
def mark_read(request, pk):
    org, _ = _scope(request)
    letter = get_object_or_404(MailLetter.objects.only("pk", "outgoing"), pk=pk, mailbox__organization=org)
    if not letter.outgoing:
        MailReadReceipt.objects.get_or_create(letter=letter, user=request.user)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"read": True})
    filters = {key: request.GET[key] for key in ("box", "folder", "q", "client", "page") if key in request.GET}
    return redirect(_url("crm_mail", org, **filters))


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
    parent = _letter(org, pk) if pk else None
    boxes = CrmMailbox.objects.filter(organization=org, enabled=True).defer("credential").order_by("address")
    selection = request.POST.get("box") or request.GET.get("box")
    if parent:
        if not parent.mailbox.enabled or (selection and _number(selection) != parent.mailbox_id):
            raise Http404
        box = parent.mailbox
    elif selection:
        box = get_object_or_404(boxes, pk=_number(selection))
    else:
        choices = list(boxes)
        if not choices:
            raise Http404
        if len(choices) > 1:
            if request.method == "POST":
                raise Http404
            return _page(request, org, "sender", boxes=choices)
        box = choices[0]
    scope = [request.user.pk, box.pk, pk]
    initial = {"token": signing.dumps([*scope, str(uuid4())], salt="mail-send")}
    personal_signature = _signature_text(request.user, org)
    initial["body"] = "\n\n" + personal_signature if personal_signature else ""
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
                destination = _url("crm_mail_detail", org, receipt.letter_id)
                if request.headers.get("X-Mail-Dialog") == "1":
                    return JsonResponse({"sent": destination})
                return redirect(destination)
            form.add_error(
                None, "Отправку подтвердить не удалось. Проверьте папку «Отправленные» перед новой попыткой."
            )
    return _page(request, org, "compose", parent=parent, form=form, box=box, signature_text=personal_signature)
