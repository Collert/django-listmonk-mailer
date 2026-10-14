import base64
import json

import httpx
import pytest
from django.core import mail
from django.core.mail import EmailMessage, EmailMultiAlternatives, InvalidMailer

from django_listmonk_mailer import EmailBackend, ListmonkError

BACKEND = "django_listmonk_mailer.EmailBackend"
OPTIONS = {
    "url": "https://listmonk.example.com/",
    "username": "api-user",
    "token": "secret-token",
    "template_id": 3,
}


class FakeListmonk:
    """Records requests and answers them like listmonk's /api/tx."""

    def __init__(self):
        self.requests = []
        self.status_code = 200
        self.response_json = {"data": True}
        self.error = None

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status_code, json=self.response_json)

    @property
    def last(self):
        return self.requests[-1]

    @property
    def last_json(self):
        return json.loads(self.last.content)


@pytest.fixture
def listmonk(monkeypatch, settings):
    fake = FakeListmonk()
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr("django_listmonk_mailer.backend.httpx.Client", client)
    settings.MAILERS = {"default": {"BACKEND": BACKEND, "OPTIONS": OPTIONS}}
    return fake


def test_send_mail_through_default_mailer(listmonk):
    sent = mail.send_mail(
        "Hello", "Plain body & stuff", None, ["Jane <jane@example.com>"]
    )

    assert sent == 1
    request = listmonk.last
    assert request.method == "POST"
    assert str(request.url) == "https://listmonk.example.com/api/tx"
    assert request.headers["Content-Type"].startswith("application/json")
    expected_auth = base64.b64encode(b"api-user:secret-token").decode()
    assert request.headers["Authorization"] == f"Basic {expected_auth}"
    assert listmonk.last_json == {
        "subscriber_emails": ["jane@example.com"],
        "subscriber_mode": "external",
        "template_id": 3,
        "subject": "Hello",
        "from_email": "Site <noreply@example.com>",
        "content_type": "plain",
        "data": {
            "subject": "Hello",
            "body": "Plain body & stuff",
            "html": "",
        },
    }


def test_html_alternative_sets_html_and_altbody(listmonk):
    message = EmailMultiAlternatives("Hi", "text version", to=["a@example.com"])
    message.attach_alternative("<p>html version</p>", "text/html")
    message.send()

    payload = listmonk.last_json
    assert payload["content_type"] == "html"
    assert payload["data"]["html"] == "<p>html version</p>"
    assert payload["data"]["body"] == "text version"
    assert payload["altbody"] == "text version"


def test_html_only_message(listmonk):
    message = EmailMessage("Hi", "<b>bold</b>", to=["a@example.com"])
    message.content_subtype = "html"
    message.send()

    payload = listmonk.last_json
    assert payload["content_type"] == "html"
    assert payload["data"] == {"subject": "Hi", "body": "", "html": "<b>bold</b>"}
    assert "altbody" not in payload


def test_all_recipients_deduplicated(listmonk):
    EmailMessage(
        "Hi",
        "body",
        to=["a@example.com", "A <A@example.com>"],
        cc=["b@example.com"],
        bcc=["c@example.com"],
    ).send()

    assert listmonk.last_json["subscriber_emails"] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]


def test_message_without_recipients_is_not_sent(listmonk):
    assert mail.mailers.default.send_messages([EmailMessage("Hi", "body")]) == 0
    assert listmonk.requests == []


def test_headers_and_reply_to(listmonk):
    EmailMessage(
        "Hi",
        "body",
        to=["a@example.com"],
        reply_to=["support@example.com"],
        headers={"X-Campaign": "welcome", "Subject": "ignored"},
    ).send()

    assert listmonk.last_json["headers"] == [
        {"X-Campaign": "welcome"},
        {"Reply-To": "support@example.com"},
    ]


def test_attachments_use_multipart(listmonk):
    message = EmailMessage("Hi", "body", to=["a@example.com"])
    message.attach("report.csv", "a,b\n1,2\n", "text/csv")
    message.attach("data.bin", b"\x00\x01")
    message.send()

    request = listmonk.last
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    content = request.content
    assert b'name="data"' in content
    assert b'"template_id": 3' in content
    assert b'name="file"; filename="report.csv"' in content
    assert b"a,b\n1,2\n" in content
    assert b'name="file"; filename="data.bin"' in content
    assert b"\x00\x01" in content


def test_per_message_overrides(listmonk, settings):
    settings.MAILERS["default"]["OPTIONS"] = OPTIONS | {"data": {"site": "Example"}}
    message = EmailMessage("Hi", "body", to=["a@example.com"])
    message.listmonk = {
        "template_id": 9,
        "messenger": "postback",
        "data": {"order_id": 1234},
    }
    message.send()

    payload = listmonk.last_json
    assert payload["template_id"] == 9
    assert payload["messenger"] == "postback"
    assert payload["data"] == {
        "site": "Example",
        "subject": "Hi",
        "body": "body",
        "html": "",
        "order_id": 1234,
    }


def test_optional_options(listmonk, settings):
    settings.MAILERS["default"]["OPTIONS"] = OPTIONS | {
        "subscriber_mode": None,
        "messenger": "email",
        "content_type": "markdown",
    }
    mail.send_mail("Hi", "**body**", None, ["a@example.com"])

    payload = listmonk.last_json
    assert "subscriber_mode" not in payload
    assert payload["messenger"] == "email"
    assert payload["content_type"] == "markdown"


def test_named_mailer(listmonk, settings):
    settings.MAILERS = {
        "default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
        "listmonk": {"BACKEND": BACKEND, "OPTIONS": OPTIONS | {"template_id": 5}},
    }
    mail.send_mail("Hi", "body", None, ["a@example.com"], using="listmonk")

    assert listmonk.last_json["template_id"] == 5
    assert mail.outbox == []


def test_reuses_one_client_inside_context_manager(listmonk):
    backend = mail.mailers.default
    with backend:
        client = backend.client
        backend.send_messages([EmailMessage("1", "b", to=["a@example.com"])])
        backend.send_messages([EmailMessage("2", "b", to=["a@example.com"])])
        assert backend.client is client
    assert backend.client is None
    assert len(listmonk.requests) == 2


def test_api_error_raises(listmonk):
    listmonk.status_code = 400
    listmonk.response_json = {"message": "Invalid template_id"}

    with pytest.raises(ListmonkError, match="HTTP 400: Invalid template_id") as info:
        mail.send_mail("Hi", "body", None, ["a@example.com"])

    assert info.value.status_code == 400
    assert isinstance(info.value, OSError)


def test_network_error_raises(listmonk):
    listmonk.error = httpx.ConnectError("connection refused")

    with pytest.raises(ListmonkError, match="Could not reach listmonk"):
        mail.send_mail("Hi", "body", None, ["a@example.com"])


def test_fail_silently_option(listmonk, settings):
    settings.MAILERS["default"]["OPTIONS"] = OPTIONS | {"fail_silently": True}
    listmonk.status_code = 500

    assert mail.send_mail("Hi", "body", None, ["a@example.com"]) == 0


@pytest.mark.parametrize(
    "options, error",
    [
        ({"url": None}, "Missing required OPTIONS: url"),
        ({"token": "", "template_id": None}, "token, template_id"),
        ({"template_id": "abc"}, "template_id must be an integer"),
        ({"subscriber_mode": "bogus"}, "subscriber_mode must be one of"),
        ({"content_type": "rtf"}, "content_type must be one of"),
        ({"data": ["x"]}, "data must be a dict"),
        ({"unknown": 1}, "Unknown options 'unknown'"),
    ],
)
def test_invalid_options(settings, options, error):
    settings.MAILERS = {
        "default": {"BACKEND": BACKEND, "OPTIONS": OPTIONS | options}
    }
    with pytest.raises(InvalidMailer, match=error) as info:
        mail.mailers.default
    assert "MAILERS['default']" in str(info.value)


def test_template_id_string_is_coerced():
    backend = EmailBackend(alias="x", **(OPTIONS | {"template_id": "7"}))
    assert backend.template_id == 7
    assert backend.url == "https://listmonk.example.com"
