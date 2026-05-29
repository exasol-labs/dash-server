"""Email delivery helpers for hosted sharing invitations."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import logging
import os
import smtplib
import ssl
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    """Normalized result for an invitation email delivery attempt."""

    status: str
    provider: str
    message_id: str | None = None
    error: str | None = None


class InvitationEmailSender:
    """Send invitation emails, or leave them for manual delivery."""

    _provider_defaults: dict[str, dict[str, Any]] = {
        "smtp": {},
        "ses": {"port": 587, "use_tls": True},
        "sendgrid": {"host": "smtp.sendgrid.net", "port": 587, "username": "apikey", "use_tls": True},
        "postmark": {"host": "smtp.postmarkapp.com", "port": 587, "use_tls": True},
        "mailgun": {"host": "smtp.mailgun.org", "port": 587, "use_tls": True},
        "resend": {"host": "smtp.resend.com", "port": 587, "username": "resend", "use_tls": True},
    }

    def __init__(self, config: dict[str, Any]) -> None:
        self.provider = str(config.get("DASH_SERVER_EMAIL_PROVIDER", "manual")).strip().lower()
        self.from_email = _optional_string(config.get("DASH_SERVER_EMAIL_FROM"))
        self.from_name = _optional_string(config.get("DASH_SERVER_EMAIL_FROM_NAME")) or "Dash Server"
        self.reply_to = _optional_string(config.get("DASH_SERVER_EMAIL_REPLY_TO"))
        self.smtp_host = _optional_string(config.get("DASH_SERVER_EMAIL_SMTP_HOST"))
        self.smtp_port = int(config.get("DASH_SERVER_EMAIL_SMTP_PORT", 587) or 587)
        self.smtp_username = _optional_string(config.get("DASH_SERVER_EMAIL_SMTP_USERNAME"))
        self.smtp_password = _optional_string(config.get("DASH_SERVER_EMAIL_SMTP_PASSWORD"))
        password_env_var = _optional_string(config.get("DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR"))
        if self.smtp_password is None and password_env_var:
            self.smtp_password = _optional_string(os.environ.get(password_env_var))
        self.smtp_allow_no_auth = _config_bool(
            config.get("DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH"),
            default=False,
        )
        self.smtp_use_tls = _config_bool(config.get("DASH_SERVER_EMAIL_SMTP_USE_TLS"), default=True)
        self.smtp_use_ssl = _config_bool(config.get("DASH_SERVER_EMAIL_SMTP_USE_SSL"), default=False)
        self.smtp_timeout_seconds = int(config.get("DASH_SERVER_EMAIL_SMTP_TIMEOUT_SECONDS", 15) or 15)
        self.ses_region = _optional_string(config.get("DASH_SERVER_EMAIL_SES_REGION")) or os.environ.get("AWS_REGION")

        defaults = self._provider_defaults.get(self.provider, {})
        self.smtp_host = self.smtp_host or defaults.get("host")
        self.smtp_port = int(config.get("DASH_SERVER_EMAIL_SMTP_PORT") or defaults.get("port") or self.smtp_port)
        self.smtp_username = self.smtp_username or defaults.get("username")
        if "use_tls" in defaults and config.get("DASH_SERVER_EMAIL_SMTP_USE_TLS") is None:
            self.smtp_use_tls = bool(defaults["use_tls"])
        if self.provider == "ses" and self.smtp_host is None and self.ses_region:
            self.smtp_host = f"email-smtp.{self.ses_region}.amazonaws.com"

    @property
    def configured(self) -> bool:
        return self.provider not in {"manual", "disabled"}

    def validate_startup(self, *, hosted_mode: bool) -> None:
        if self.provider in {"manual", "disabled"}:
            return
        if self.provider == "console":
            if not self.from_email:
                raise RuntimeError("Console email delivery requires DASH_SERVER_EMAIL_FROM.")
            return
        if self.provider not in self._provider_defaults:
            raise RuntimeError(
                "DASH_SERVER_EMAIL_PROVIDER must be manual, console, smtp, ses, sendgrid, postmark, mailgun, or resend."
            )
        if not self.from_email:
            raise RuntimeError("Email delivery requires DASH_SERVER_EMAIL_FROM.")
        if not self.smtp_host:
            raise RuntimeError("Email delivery requires DASH_SERVER_EMAIL_SMTP_HOST or provider-specific host config.")
        if self.provider == "smtp" and self.smtp_allow_no_auth:
            if bool(self.smtp_username) != bool(self.smtp_password):
                raise RuntimeError(
                    "Email delivery with DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH=true requires both "
                    "DASH_SERVER_EMAIL_SMTP_USERNAME and DASH_SERVER_EMAIL_SMTP_PASSWORD, or neither."
                )
        else:
            if not self.smtp_username:
                raise RuntimeError("Email delivery requires DASH_SERVER_EMAIL_SMTP_USERNAME.")
            if not self.smtp_password:
                raise RuntimeError(
                    "Email delivery requires DASH_SERVER_EMAIL_SMTP_PASSWORD or DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR."
                )
        if hosted_mode and self.smtp_use_ssl is False and self.smtp_use_tls is False:
            raise RuntimeError("Hosted email delivery requires TLS or SSL for SMTP.")

    def send_invitation(
        self,
        *,
        app_title: str,
        recipient_email: str,
        accept_url: str,
        role: str,
        scope: str,
        expires_at: str,
        inviter_display_name: str | None = None,
        message: str | None = None,
    ) -> EmailDeliveryResult:
        if self.provider in {"manual", "disabled"}:
            return EmailDeliveryResult(status="pending_manual_delivery", provider=self.provider)
        email = self._invitation_message(
            app_title=app_title,
            recipient_email=recipient_email,
            accept_url=accept_url,
            role=role,
            scope=scope,
            expires_at=expires_at,
            inviter_display_name=inviter_display_name,
            message=message,
        )
        if self.provider == "console":
            LOGGER.info("Invitation email prepared for console delivery: %s", email.as_string())
            return EmailDeliveryResult(status="sent", provider="console", message_id=email["Message-ID"])
        try:
            with self._smtp_client() as client:
                if self.smtp_username and self.smtp_password:
                    client.login(self.smtp_username, self.smtp_password)
                response = client.send_message(email)
        except Exception as exc:  # pragma: no cover - concrete SMTP exceptions vary by provider.
            LOGGER.warning("Invitation email delivery failed through %s: %s", self.provider, exc)
            return EmailDeliveryResult(status="failed", provider=self.provider, error=str(exc))
        failed_recipients = ", ".join(response.keys()) if response else None
        if failed_recipients:
            return EmailDeliveryResult(
                status="failed",
                provider=self.provider,
                error=f"SMTP rejected recipients: {failed_recipients}",
            )
        return EmailDeliveryResult(status="sent", provider=self.provider, message_id=email["Message-ID"])

    def _smtp_client(self):
        assert self.smtp_host is not None
        context = ssl.create_default_context()
        if self.smtp_use_ssl:
            return smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                timeout=self.smtp_timeout_seconds,
                context=context,
            )
        client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout_seconds)
        if self.smtp_use_tls:
            client.starttls(context=context)
        return client

    def _invitation_message(
        self,
        *,
        app_title: str,
        recipient_email: str,
        accept_url: str,
        role: str,
        scope: str,
        expires_at: str,
        inviter_display_name: str | None,
        message: str | None,
    ) -> EmailMessage:
        if not self.from_email:
            raise RuntimeError("Email sender address is not configured.")
        subject = f"You're invited to {app_title}"
        access_summary = "preview dashboard" if scope == "preview" else "live dashboard"
        plain_lines = [
            f"You have been invited to access {app_title}.",
            "",
            f"Access: {access_summary}",
            f"Role: {role}",
            f"Scope: {scope}",
            f"Expires: {expires_at} UTC",
            "",
            "Open this link to accept the invitation:",
            accept_url,
        ]
        if inviter_display_name:
            plain_lines[2:2] = [f"Invited by: {inviter_display_name}"]
        if message:
            plain_lines.extend(["", "Message from the inviter:", message])
        inviter_html = (
            f"<p>Invited by: <strong>{_html_escape(inviter_display_name)}</strong></p>"
            if inviter_display_name
            else ""
        )
        html_message = f"<p>{_html_escape(message)}</p>" if message else ""
        html = f"""
        <html>
          <body>
            <p>You have been invited to access <strong>{_html_escape(app_title)}</strong>.</p>
            {inviter_html}
            <p><a href="{_html_escape(accept_url)}">Accept invitation</a></p>
            <p>Access: {_html_escape(access_summary)}<br>Role: {_html_escape(role)}<br>Scope: {_html_escape(scope)}<br>Expires: {_html_escape(expires_at)} UTC</p>
            {html_message}
          </body>
        </html>
        """
        email = EmailMessage()
        email["Subject"] = subject
        email["From"] = f"{self.from_name} <{self.from_email}>"
        email["To"] = recipient_email
        email["Message-ID"] = f"<dash-server-invite-{os.urandom(12).hex()}@dash-server>"
        if self.reply_to:
            email["Reply-To"] = self.reply_to
        email.set_content("\n".join(plain_lines))
        email.add_alternative(html, subtype="html")
        return email


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _config_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
