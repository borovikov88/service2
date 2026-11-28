"""Mail transports authenticate only after verified TLS to the vetted DNS destination."""

import imaplib
import re
import smtplib
import ssl
import time
from contextlib import contextmanager

from django.views.decorators.debug import sensitive_variables

from pool_service.avito_services_media import _numeric_socket, _resolve

PRESETS = {
    "mailru": ("imap.mail.ru", "smtp.mail.ru"),
    "yandex": ("imap.yandex.ru", "smtp.yandex.ru"),
    "gmail": ("imap.gmail.com", "smtp.gmail.com"),
}


def public_hostname(value):
    host = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2 or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1])
            or host.endswith((".local", ".internal", ".localhost"))
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise ValueError("Invalid mail hostname")
    return host


def _socket(host, port, timeout, tls_context=None):
    deadline = time.monotonic() + timeout
    try:
        name = public_hostname(host)
        addresses = _resolve(name, port, deadline, all_addresses=True)
    except ValueError as exc:
        raise OSError("Недоступный адрес сервера почты.") from exc
    for index, address in enumerate(addresses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Время подключения к почте истекло.")
        try:
            sock = _numeric_socket(address, port, remaining / (len(addresses) - index))
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            break
        except OSError:
            if index == len(addresses) - 1:
                raise
    if tls_context is None:
        return sock
    try:
        return tls_context.wrap_socket(sock, server_hostname=name)
    except BaseException:
        sock.close()
        raise


class _Imap(imaplib.IMAP4):
    def __init__(self, host, port, context):
        self.mail_tls = context if port == 993 else None
        super().__init__(host, port, timeout=10)

    def _create_socket(self, timeout):
        return _socket(self.host, self.port, timeout, self.mail_tls)


class _Smtp(smtplib.SMTP):
    def __init__(self, host, port, context):
        self.mail_tls = context if port == 465 else None
        super().__init__(host, port, timeout=10)

    def _get_socket(self, host, port, timeout):
        return _socket(host, port, timeout, self.mail_tls)


@sensitive_variables("password")
@contextmanager
def imap_session(box, password):
    if box.imap_port not in (993, 143):
        raise OSError("Неизвестный защищённый порт IMAP.")
    context = ssl.create_default_context()
    client = _Imap(box.imap_host, box.imap_port, context)
    try:
        if box.imap_port == 143:
            client.starttls(ssl_context=context)
        client.login(box.login or box.address, password)
        yield client
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            try:
                client.shutdown()
            except OSError:
                pass


@sensitive_variables("password")
@contextmanager
def smtp_session(box, password):
    if box.smtp_port not in (465, 587):
        raise OSError("Неизвестный защищённый порт SMTP.")
    context = ssl.create_default_context()
    with _Smtp(box.smtp_host, box.smtp_port, context) as client:
        if box.smtp_port == 587:
            client.starttls(context=context)
        client.login(box.login or box.address, password)
        yield client
