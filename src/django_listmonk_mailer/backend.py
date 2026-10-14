"""Django email backend that delivers mail through listmonk's transactional API."""

import json
import mimetypes
from email.message import Message
from email.utils import parseaddr

import httpx
from django.core.mail import InvalidMailer
from django.core.mail.backends.base import BaseEmailBackend
from django.core.serializers.json import DjangoJSONEncoder

TX_ENDPOINT = "/api/tx"
CONTENT_TYPES = {"html", "markdown", "plain"}
SUBSCRIBER_MODES = {"default", "fallback", "external"}


class ListmonkError(OSError):
    """
    listmonk rejected a message, or could not be reached.

    Subclasses OSError (like smtplib.SMTPException) so that code catching
    network-related mail errors handles it too.
    """

    def __init__(self, message, *, status_code=None, response=None, email_message=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        self.email_message = email_message


class EmailBackend(BaseEmailBackend):
    """
    Send Django EmailMessages through listmonk's ``POST /api/tx`` endpoint.

    Configure it as a MAILERS entry::

        MAILERS = {
            "default": {
                "BACKEND": "django_listmonk_mailer.EmailBackend",
                "OPTIONS": {
                    "url": "https://listmonk.example.com",
                    "username": "api-user",
                    "token": "...",
                    "template_id": 3,
                },
            },
        }
    """

    def __init__(
        self,
        url=None,
        username=None,
        token=None,
        template_id=None,
        subscriber_mode="external",
        messenger=None,
        content_type=None,
        data=None,
        timeout=10.0,
        verify=True,
        fail_silently=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.fail_silently = fail_silently

        missing = [
            name
            for name, value in [
                ("url", url),
                ("username", username),
                ("token", token),
                ("template_id", template_id),
            ]
            if value in (None, "")
        ]
        if missing:
            raise InvalidMailer(
                f"Missing required OPTIONS: {', '.join(missing)}.", alias=self.alias
            )
        try:
            template_id = int(template_id)
        except (TypeError, ValueError):
            raise InvalidMailer(
                f"template_id must be an integer, got {template_id!r}.",
                alias=self.alias,
            ) from None
        if subscriber_mode is not None and subscriber_mode not in SUBSCRIBER_MODES:
            raise InvalidMailer(
                f"subscriber_mode must be one of {sorted(SUBSCRIBER_MODES)} or None, "
                f"got {subscriber_mode!r}.",
                alias=self.alias,
            )
        if content_type is not None and content_type not in CONTENT_TYPES:
            raise InvalidMailer(
                f"content_type must be one of {sorted(CONTENT_TYPES)} or None, "
                f"got {content_type!r}.",
                alias=self.alias,
            )
        if data is not None and not isinstance(data, dict):
            raise InvalidMailer("data must be a dict.", alias=self.alias)

        self.url = url.rstrip("/")
        self.username = username
        self.token = token
        self.template_id = template_id
        self.subscriber_mode = subscriber_mode
        self.messenger = messenger
        self.content_type = content_type
        self.data = data or {}
        self.timeout = timeout
        self.verify = verify
        self.client = None

    def open(self):
        """
        Create the HTTP client. Return True if a new client was created, so
        callers know to close it again.
        """
        if self.client is not None:
            return False
        self.client = httpx.Client(
            base_url=self.url,
            auth=(self.username, self.token),
            timeout=self.timeout,
            verify=self.verify,
        )
        return True

    def close(self):
        if self.client is None:
            return
        try:
            self.client.close()
        finally:
            self.client = None

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        new_client = self.open()
        try:
            return sum(1 for message in email_messages if self._send(message))
        finally:
            if new_client:
                self.close()

    def _send(self, message):
        recipients = self.get_recipients(message)
        if not recipients:
            return False
        payload = self.build_payload(message, recipients)
        files = self.get_files(message)
        body = json.dumps(payload, cls=DjangoJSONEncoder)
        try:
            if files:
                # listmonk takes attachments as multipart/form-data, with the
                # JSON request in a "data" field and one "file" field per file.
                response = self.client.post(
                    TX_ENDPOINT, data={"data": body}, files=files
                )
            else:
                response = self.client.post(
                    TX_ENDPOINT,
                    content=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
        except httpx.HTTPError as error:
            if self.fail_silently:
                return False
            raise ListmonkError(
                f"Could not reach listmonk at {self.url}: {error}",
                email_message=message,
            ) from error

        if response.is_error:
            if self.fail_silently:
                return False
            raise ListmonkError(
                f"listmonk returned HTTP {response.status_code}: "
                f"{self._error_detail(response)}",
                status_code=response.status_code,
                response=response,
                email_message=message,
            )
        return True

    def get_recipients(self, message):
        """
        Return the bare addresses of all to, cc and bcc recipients, without
        duplicates. listmonk delivers a separate copy to each of them.
        """
        seen = {}
        for recipient in message.recipients():
            address = parseaddr(str(recipient))[1]
            if address:
                seen.setdefault(address.lower(), address)
        return list(seen.values())

    def build_payload(self, message, recipients):
        text, html = self.get_bodies(message)
        payload = {
            "subscriber_emails": recipients,
            "template_id": self.template_id,
            "subject": str(message.subject),
            "data": {
                **self.data,
                "subject": str(message.subject),
                "body": text,
                "html": html,
            },
            "content_type": self.content_type or ("html" if html else "plain"),
        }
        if message.from_email:
            payload["from_email"] = str(message.from_email)
        if self.subscriber_mode:
            payload["subscriber_mode"] = self.subscriber_mode
        if self.messenger:
            payload["messenger"] = self.messenger
        if html and text:
            payload["altbody"] = text

        headers = self.get_headers(message)
        if headers:
            payload["headers"] = headers

        # Per-message overrides: message.listmonk = {"template_id": 7, ...}.
        overrides = dict(getattr(message, "listmonk", None) or {})
        if "data" in overrides:
            payload["data"].update(overrides.pop("data"))
        payload.update(overrides)
        return payload

    def get_bodies(self, message):
        """Return the (plain text, html) bodies of a message."""
        text = html = ""
        if getattr(message, "content_subtype", "plain") == "html":
            html = message.body or ""
        else:
            text = message.body or ""
        for content, mimetype in getattr(message, "alternatives", None) or []:
            if mimetype == "text/html" and not html:
                html = content
            elif mimetype == "text/plain" and not text:
                text = content
        if isinstance(text, bytes):
            text = text.decode()
        if isinstance(html, bytes):
            html = html.decode()
        return str(text), str(html)

    def get_headers(self, message):
        # listmonk sets these itself from the request fields.
        skip = {"from", "to", "cc", "bcc", "subject"}
        headers = {
            name: str(value)
            for name, value in (message.extra_headers or {}).items()
            if name.lower() not in skip
        }
        if message.reply_to:
            headers = {k: v for k, v in headers.items() if k.lower() != "reply-to"}
            headers["Reply-To"] = ", ".join(str(a) for a in message.reply_to)
        return [{name: value} for name, value in headers.items()]

    def get_files(self, message):
        files = []
        for attachment in message.attachments:
            if isinstance(attachment, Message):
                filename = attachment.get_filename() or "attachment"
                content = attachment.get_payload(decode=True) or b""
                mimetype = attachment.get_content_type()
            else:
                filename, content, mimetype = attachment
                filename = filename or "attachment"
                if isinstance(content, str):
                    content = content.encode()
                if not mimetype:
                    mimetype = (
                        mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    )
            files.append(("file", (filename, content, mimetype)))
        return files

    @staticmethod
    def _error_detail(response):
        try:
            detail = response.json().get("message")
        except (ValueError, AttributeError):
            detail = None
        return detail or response.text[:500] or response.reason_phrase
