# django-listmonk-mailer

A Django email backend that sends mail through [listmonk](https://listmonk.app)'s
[transactional API](https://listmonk.app/docs/apis/transactional/), built for
Django 6.1's [`MAILERS` setting](https://docs.djangoproject.com/en/6.1/howto/mailers-migration/).

Once it is configured, `send_mail()`, `EmailMessage.send()`, `mail_admins()`,
password-reset emails and everything else in Django that sends email go out
through listmonk.

## Requirements

- Python 3.12+
- Django 6.1+
- A listmonk instance with an API user and a transactional template

## Installation

```bash
pip install django-listmonk-mailer
```

## Setup

### 1. Create an API user in listmonk

In listmonk, go to **Admin → Users**, create an **API** user and copy its token.
Give it a role that is allowed to send transactional messages (`tx:send`).

### 2. Create a transactional template

Go to **Campaigns → Templates → New**, choose the **Transactional** type and use
this as the body:

```
{{ if .Tx.Data.html }}{{ Safe .Tx.Data.html }}{{ else }}{{ Safe .Tx.Data.body }}{{ end }}
```

Set the template's subject to `{{ .Tx.Data.subject }}` (the backend also sends
the subject as an override, so this is only a fallback). Note the template's ID.

The backend sends this in the template's `.Tx.Data`:

| Key       | Contents                                                  |
|-----------|-----------------------------------------------------------|
| `subject` | The message subject                                       |
| `body`    | The plain-text body (empty for HTML-only messages)        |
| `html`    | The HTML body or `text/html` alternative (empty if none)  |

plus anything from the `data` option and per-message `data` (see below). You
can also build a fully designed template that uses only your own `data` keys.

### 3. Configure `MAILERS`

```python
# settings.py
MAILERS = {
    "default": {
        "BACKEND": "django_listmonk_mailer.EmailBackend",
        "OPTIONS": {
            "url": "https://listmonk.example.com",
            "username": "django-api",
            "token": env("LISTMONK_TOKEN"),
            "template_id": 3,
        },
    },
}
```

Then send mail as usual:

```python
from django.core import mail

mail.send_mail("Welcome!", "Thanks for signing up.", None, ["jane@example.com"])
```

## Options

| Option            | Default      | Description |
|-------------------|--------------|-------------|
| `url`             | *required*   | Base URL of your listmonk instance. |
| `username`        | *required*   | listmonk API user name. |
| `token`           | *required*   | listmonk API user token. |
| `template_id`     | *required*   | ID of the transactional template to render messages with. |
| `subscriber_mode` | `"external"` | `"external"` sends to any address without looking it up; `"fallback"` uses subscriber data when the address is a subscriber and sends anyway when it isn't; `"default"` only sends to existing subscribers. `None` omits the field, for listmonk versions that do not support it. |
| `messenger`       | `None`       | listmonk messenger to send through (listmonk defaults to `email`). |
| `content_type`    | `None`       | `"html"`, `"markdown"` or `"plain"`. By default it is `"html"` when the message has an HTML body and `"plain"` otherwise. |
| `data`            | `{}`         | Extra values added to `.Tx.Data` on every message, e.g. `{"site_name": "Example"}`. |
| `timeout`         | `10.0`       | HTTP timeout in seconds. |
| `verify`          | `True`       | TLS verification, passed to [httpx](https://www.python-httpx.org/advanced/ssl/). |
| `fail_silently`   | `False`      | Ignore listmonk and network errors instead of raising them. |

## Multiple configurations

Use named mailers for different templates or listmonk instances, and select
one with `using`:

```python
MAILERS = {
    "default": {
        "BACKEND": "django_listmonk_mailer.EmailBackend",
        "OPTIONS": {**LISTMONK, "template_id": 3},
    },
    "receipts": {
        "BACKEND": "django_listmonk_mailer.EmailBackend",
        "OPTIONS": {**LISTMONK, "template_id": 8},
    },
    # Error emails that should never raise.
    "admin-logging": {
        "BACKEND": "django_listmonk_mailer.EmailBackend",
        "OPTIONS": {**LISTMONK, "template_id": 3, "fail_silently": True},
    },
}

mail.send_mail("Your receipt", body, None, [user.email], using="receipts")
```

## Per-message options

Set a `listmonk` dict on a message to override any field of the
[`/api/tx` request](https://listmonk.app/docs/apis/transactional/) for that
message. `data` is merged into the default `.Tx.Data` rather than replacing it:

```python
from django.core.mail import EmailMessage

message = EmailMessage("Order shipped", "Your order is on its way.", to=[user.email])
message.listmonk = {
    "template_id": 12,
    "data": {"order_id": order.pk, "tracking_url": order.tracking_url},
}
message.send()
```

## How messages are mapped

- **Recipients**: `to`, `cc` and `bcc` addresses are combined and deduplicated
  into `subscriber_emails`. listmonk sends each recipient their own copy, so
  recipients never see each other's addresses and there is no `Cc` header.
- **From**: `from_email` (or `DEFAULT_FROM_EMAIL`) is sent as `from_email`.
- **Bodies**: an HTML alternative (`attach_alternative(..., "text/html")`) or an
  HTML body (`content_subtype = "html"`) is sent as `.Tx.Data.html`. When a
  message has both plain text and HTML, the plain text is also sent as `altbody`
  for a multipart email.
- **Headers**: `reply_to` and `extra_headers` are sent as `headers`.
  `From`, `To`, `Cc`, `Bcc` and `Subject` headers are dropped, because listmonk
  sets those.
- **Attachments**: sent as `multipart/form-data` file uploads.

## Errors

Failures raise `django_listmonk_mailer.ListmonkError`, which has `status_code`,
`response` and `email_message` attributes. It subclasses `OSError`, like
`smtplib.SMTPException`, so the
[recommended pattern](https://docs.djangoproject.com/en/6.1/howto/mailers-migration/)
of catching `OSError` for delivery problems works unchanged:

```python
try:
    mail.send_mail(...)
except OSError:
    logger.warning("Could not send email", exc_info=True)
```

Invalid `OPTIONS` raise `django.core.mail.InvalidMailer` when the mailer is
first used.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

## License

MIT
