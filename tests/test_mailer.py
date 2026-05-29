from __future__ import annotations

import smtplib

from dash_server.mailer import InvitationEmailSender


class _CapturingSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.kwargs = kwargs
        self.starttls_called = False
        self.login_args = None
        self.messages = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def starttls(self, context=None):
        self.starttls_called = True
        return None

    def login(self, username, password):
        self.login_args = (username, password)
        return None

    def send_message(self, message):
        self.messages.append(message)
        return {}


class _CapturingSMTPSSL(_CapturingSMTP):
    pass


def test_mailer_sends_invitation_via_mocked_smtp(monkeypatch):
    _CapturingSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _CapturingSMTP)
    sender = InvitationEmailSender(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_FROM_NAME": "Dash Server",
            "DASH_SERVER_EMAIL_REPLY_TO": "owners@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp.example.test",
            "DASH_SERVER_EMAIL_SMTP_USERNAME": "dash",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD": "secret",
        }
    )

    sender.validate_startup(hosted_mode=True)
    result = sender.send_invitation(
        app_title="Executive Overview",
        recipient_email="external@example.test",
        accept_url="https://dash.example.test/share/invitations/abc123",
        role="viewer",
        scope="live",
        expires_at="2026-04-20T10:00:00",
        inviter_display_name="Admin User",
        message="Please review <safely>.",
    )

    assert result.status == "sent"
    assert result.provider == "smtp"
    assert result.message_id is not None

    client = _CapturingSMTP.instances[0]
    assert client.host == "smtp.example.test"
    assert client.port == 587
    assert client.starttls_called is True
    assert client.login_args == ("dash", "secret")
    assert len(client.messages) == 1

    message = client.messages[0]
    plain_body = message.get_body(preferencelist=("plain",)).get_content()
    html_body = message.get_body(preferencelist=("html",)).get_content()

    assert message["Subject"] == "You're invited to Executive Overview"
    assert message["From"] == "Dash Server <dash@example.test>"
    assert message["Reply-To"] == "owners@example.test"
    assert "Invited by: Admin User" in plain_body
    assert "Access: live dashboard" in plain_body
    assert "https://dash.example.test/share/invitations/abc123" in plain_body
    assert "Please review <safely>." in plain_body
    assert "Please review &lt;safely&gt;." in html_body


def test_mailer_supports_no_auth_smtp_relays(monkeypatch):
    _CapturingSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _CapturingSMTP)
    sender = InvitationEmailSender(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp-relay.example.test",
            "DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH": True,
        }
    )

    sender.validate_startup(hosted_mode=True)
    result = sender.send_invitation(
        app_title="Finance Review",
        recipient_email="external@example.test",
        accept_url="https://dash.example.test/share/invitations/no-auth",
        role="viewer",
        scope="preview",
        expires_at="2026-04-20T10:00:00",
    )

    assert result.status == "sent"
    client = _CapturingSMTP.instances[0]
    assert client.starttls_called is True
    assert client.login_args is None


def test_mailer_uses_provider_defaults_and_password_env_var(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "sendgrid-secret")
    sender = InvitationEmailSender(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "sendgrid",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR": "SENDGRID_API_KEY",
        }
    )

    assert sender.smtp_host == "smtp.sendgrid.net"
    assert sender.smtp_port == 587
    assert sender.smtp_username == "apikey"
    assert sender.smtp_password == "sendgrid-secret"
    sender.validate_startup(hosted_mode=True)


def test_mailer_uses_ssl_transport_when_configured(monkeypatch):
    _CapturingSMTPSSL.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _CapturingSMTPSSL)
    sender = InvitationEmailSender(
        {
            "DASH_SERVER_EMAIL_PROVIDER": "smtp",
            "DASH_SERVER_EMAIL_FROM": "dash@example.test",
            "DASH_SERVER_EMAIL_SMTP_HOST": "smtp.example.test",
            "DASH_SERVER_EMAIL_SMTP_PORT": 465,
            "DASH_SERVER_EMAIL_SMTP_USERNAME": "dash",
            "DASH_SERVER_EMAIL_SMTP_PASSWORD": "secret",
            "DASH_SERVER_EMAIL_SMTP_USE_TLS": False,
            "DASH_SERVER_EMAIL_SMTP_USE_SSL": True,
        }
    )

    sender.validate_startup(hosted_mode=True)
    result = sender.send_invitation(
        app_title="Operations",
        recipient_email="external@example.test",
        accept_url="https://dash.example.test/share/invitations/ssl",
        role="viewer",
        scope="live",
        expires_at="2026-04-20T10:00:00",
    )

    assert result.status == "sent"
    client = _CapturingSMTPSSL.instances[0]
    assert client.port == 465
    assert client.starttls_called is False
    assert client.login_args == ("dash", "secret")
