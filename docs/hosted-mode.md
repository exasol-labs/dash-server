# Hosted Mode Admin Guide

This guide explains how to run `dash-server` as a hosted multi-user service instead of the default local single-user mode.

Hosted mode changes the server behavior in four important ways:

- browser requests to dashboards are authorized before mounted Dash apps run
- the `/` catalog is filtered per viewer
- sharing is enforced through ACLs, public policy, one-time links, and invitations
- the `/mcp` control plane is restricted to authenticated `admin`, `owner`, or `editor` users

## Choose The Right Deployment Mode

`DASH_SERVER_MODE` supports:

- `local`: default. No hosted auth or hosted authorization setup is required.
- `hosted`: requires explicit auth configuration and secure cookie/base-URL settings. The server fails closed if required settings are missing.

Use `hosted` only when the server is behind HTTPS and you are ready to manage authentication and sharing policy.

## Current Production Recommendation

Use `DASH_SERVER_AUTH_PROVIDER=trusted_proxy` for real hosted deployments today.

Direct `oidc` mode is present, but it is still scaffold-level:

- `/auth/login` builds the redirect correctly
- `/auth/callback` validates `state` and `nonce`
- production token exchange and ID-token signature validation are not implemented yet

That means direct `oidc` is not the recommended production path yet. If you need enterprise SSO now, terminate auth at a trusted reverse proxy or identity-aware gateway and pass identity headers to `dash-server`.

## Hosted Mode Prerequisites

These settings are required in hosted mode:

- `DASH_SERVER_MODE=hosted`
- `SECRET_KEY` or `DASH_SERVER_SECRET_KEY`
- `SESSION_COOKIE_SECURE=true`
- `SESSION_COOKIE_HTTPONLY=true`
- `SESSION_COOKIE_SAMESITE=Lax`, `Strict`, or `None`
- `DASH_SERVER_PUBLIC_BASE_URL=https://your-public-hostname`

Hosted mode also requires an auth provider:

- `DASH_SERVER_AUTH_PROVIDER=trusted_proxy`
- or `DASH_SERVER_AUTH_PROVIDER=oidc`

## Network and Port Settings

The control-plane HTTP listener defaults to `127.0.0.1:5100`.

Use CLI flags for ad hoc runs:

```bash
dash-server --host 127.0.0.1 --port 5200
```

Use environment variables for repeatable service definitions:

```bash
DASH_SERVER_HOST=127.0.0.1
DASH_SERVER_PORT=5200
```

When you change the control-plane port, update every browser, curl, reverse
proxy, and MCP client URL. For example, with `DASH_SERVER_PORT=5200`, the local
MCP endpoint is `http://127.0.0.1:5200/mcp`.

On macOS Monterey 12 and later, AirPlay Receiver can bind ports `5000` and
`7000` through system ControlCenter / AirPlay helper processes. `dash-server`
defaults to `5100` instead of Flask's classic `5000` to avoid that local macOS
collision. If you explicitly run `dash-server --port 5000` and startup fails,
either disable AirPlay Receiver in macOS sharing settings or choose another
port.

When `DASH_SERVER_APP_RUNTIME_MODE=isolated`, hosted app workers also bind
loopback HTTP ports behind the control-plane proxy. By default the OS chooses
free ephemeral worker ports. To restrict those worker ports for firewall rules,
test harnesses, or local conflict avoidance, set an inclusive range:

```bash
DASH_SERVER_APP_WORKER_PORT_RANGE=5500-5599
```

Do not include `5000` or `7000` in `DASH_SERVER_APP_WORKER_PORT_RANGE` on Macs
where AirPlay Receiver is enabled.

## Minimum Hosted Configuration

Example hosted baseline:

```bash
DASH_SERVER_MODE=hosted
DASH_SERVER_SECRET_KEY="replace-with-a-long-random-secret"
DASH_SERVER_PUBLIC_BASE_URL="https://dash.example.com"

DASH_SERVER_SESSION_COOKIE_SECURE=true
DASH_SERVER_SESSION_COOKIE_HTTPONLY=true
DASH_SERVER_SESSION_COOKIE_SAMESITE=Lax
```

If these are wrong, startup fails immediately. That is intentional.

## Choose An Authentication Provider

### Trusted Proxy

Use this when an upstream component already authenticates the user and injects identity headers. Typical examples are:

- `oauth2-proxy`
- Cloudflare Access
- Google IAP
- an enterprise ingress/gateway
- a custom reverse proxy with strict header stripping and rewriting

Required settings:

```bash
DASH_SERVER_AUTH_PROVIDER=trusted_proxy
DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED=true
DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS="10.0.0.0/8,192.168.0.0/16"
```

Optional header overrides:

```bash
DASH_SERVER_TRUSTED_PROXY_USER_HEADER="X-Forwarded-User"
DASH_SERVER_TRUSTED_PROXY_EMAIL_HEADER="X-Forwarded-Email"
DASH_SERVER_TRUSTED_PROXY_GROUPS_HEADER="X-Forwarded-Groups"
```

Requirements for safe use:

- `dash-server` must not trust these headers directly from the public internet
- the proxy must strip any incoming copies of the same headers before setting its own values
- `DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS` must include only the proxy source IPs or networks

In trusted-proxy mode, a browser user principal looks like:

- `principal_id=trusted_proxy:{user-header-value}`

Example:

- `trusted_proxy:alice@example.com`
- or `trusted_proxy:alice`

### Direct OIDC

Use this only for testing or controlled internal evaluation until production token validation is implemented.

Required settings:

```bash
DASH_SERVER_AUTH_PROVIDER=oidc
DASH_SERVER_OIDC_ISSUER="https://issuer.example.com"
DASH_SERVER_OIDC_CLIENT_ID="your-client-id"
DASH_SERVER_OIDC_REDIRECT_URI="https://dash.example.com/auth/callback"
```

Optional settings:

```bash
DASH_SERVER_OIDC_AUTHORIZATION_ENDPOINT="https://issuer.example.com/authorize"
DASH_SERVER_OIDC_SCOPES="openid email profile"
DASH_SERVER_OIDC_GROUPS_CLAIM="groups"
DASH_SERVER_OIDC_ORG_CLAIM="tenant"
```

Testing-only callback mode:

```bash
DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS=true
```

Do not enable `DASH_SERVER_OIDC_ACCEPT_TEST_TOKENS` in production.

In OIDC mode, the effective principal id format is:

- `{issuer}:{subject}`

If you bootstrap an admin in direct OIDC mode, you must use the exact issuer and subject combination returned by the provider.

## Configure A Bootstrap Admin

Hosted mode needs an initial administrator so someone can use `/mcp` and create durable sharing grants.

Use:

```bash
DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS="trusted_proxy:alice@example.com"
```

You can provide multiple comma-separated values.

What bootstrap admin does:

- grants global `admin`, `owner`, `editor`, and `viewer` roles at request resolution
- allows the first administrator to reach `/mcp`
- allows the first administrator to create app owner/editor/viewer grants

Recommended operational pattern:

1. Configure one or two bootstrap admin principals.
2. Start the server.
3. Log in through the chosen auth path.
4. Create durable app owner/admin grants through the sharing tools.
5. Remove or minimize bootstrap admins after normal ownership is in place.

## First Startup Checklist

Before exposing the server publicly, verify:

1. HTTPS terminates in front of the server.
2. `DASH_SERVER_PUBLIC_BASE_URL` matches the external HTTPS origin.
3. Hosted cookies are secure and `HttpOnly`.
4. Auth provider settings are present.
5. A bootstrap admin principal is configured.
6. Public dashboards are still disabled unless you explicitly want them.

## Verify The Identity Layer

After startup:

1. Open `/auth/whoami` as an anonymous user. It should show `anonymous` in hosted mode.
2. Access the server through your configured auth path.
3. Open `/auth/whoami` again.
4. Confirm the returned principal id, email, groups, and roles are what you expect.

If you configured `DASH_SERVER_BOOTSTRAP_ADMIN_PRINCIPAL_IDS`, confirm the principal includes `admin`.

## What Hosted Mode Enforces

Hosted mode protects:

- live dashboards under `/apps/{name}`
- Dash layout and dependency endpoints under mounted app prefixes
- Dash callback POST endpoints
- mounted app asset routes
- preview dashboards under `/preview/{name}/{revision}`
- the catalog at `/`
- the `/mcp` control plane

Important current behavior:

- non-public dashboards require authentication plus a matching grant or global role
- preview routes are stricter than live routes
- hosted `viewer` does not automatically get preview access
- hosted `/mcp` is denied to anonymous users, viewer-only users, and link principals

## What Hosted Mode Does Not Have: The Browser Session Channel

The browser session channel — `app_session_eval_js`, which runs ephemeral JavaScript in a
user's live dashboard tab so an agent can read current selections and what is visible —
is **unavailable in hosted mode** and cannot be switched on.

Hosted pages contain none of its client code, its routes are not registered, and the tool
returns `session_channel_unavailable` with `reason: "hosted_mode"`. That is deliberate,
not an oversight, for two reasons:

1. **It would be surveillance of a third party.** In local mode the person looking at the
   dashboard is the person driving the MCP client, so inspecting "the session" means
   inspecting your own tab. In hosted mode an operator would be reading and modifying
   another named person's live browser session, which needs a consent model this
   implementation does not have.
2. **`/mcp` currently authenticates from the Flask session cookie alone** — no bearer
   token, no CSRF token, and no `Origin` / `Sec-Fetch-Site` check — and dashboards are
   served from the same origin as the control plane. Any JavaScript running in a
   dashboard page can therefore call `/mcp` with the viewer's cookies at the viewer's
   role. That exposure already exists for anyone who can deploy app code or land an XSS;
   a hosted session channel would sit directly inside it.

Hardening `/mcp` against same-origin browser callers is a prerequisite for ever
supporting the channel in hosted mode. Until then, hosted debugging uses the
server-side signals: `app_collect_diagnostics`, `app_tail_logs`, `app_inspect_traceback`,
and `app_run_healthcheck`.

## Public Dashboard Policy

Public anonymous dashboard access is off by default.

To allow anonymous access to a live dashboard, all of these must be true:

- server policy: `DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED=true`
- app visibility: `visibility=public`
- app share policy: `link_scope=public`
- app auth policy is not `required`

Enable the server-side public policy only if you are comfortable allowing administrators and app owners to expose dashboards anonymously.

Example:

```bash
DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED=true
```

Without that setting, an app marked public at the app layer still will not be served anonymously.

## Sharing Model Administrators Need To Understand

Current hosted sharing supports:

- user grants
- group grants
- domain grants
- organization grants
- public policy
- one-time links
- external invitations

Current roles:

- `viewer`
- `preview_viewer`
- `editor`
- `owner`

Important operational rules:

- sharing grants are stored in SQLite, not Git desired state
- one-time links are URL-only by default and do not make dashboards discoverable in `/`
- accepted email invitations create durable external user grants, so invited users can later see shared dashboards in `/`

## Catalog Behavior

The root catalog at `/` is filtered per viewer:

- anonymous users see only dashboards that are truly public
- authenticated users see only dashboards they can discover
- preview links are shown only when the viewer can access preview

This means a user may be able to access one dashboard and still not see another dashboard’s title or route at all.

## `/mcp` Control Plane Access

Hosted `/mcp` is not open to every authenticated user.

Today it requires:

- an authenticated `user` principal
- and one of `admin`, `owner`, or `editor`

Denied principals include:

- anonymous users
- viewer-only users
- one-time-link principals

Operationally, treat `/mcp` as an administrative/control-plane surface, not a general end-user API.

## Email Invitations And One-Time Links

Hosted mode supports two external-sharing flows:

- one-time links: temporary URL-only access
- external invitations: emailed acceptance links that create durable external viewer access

One-time links:

- are created per app
- are single-use by default
- store only token hashes in SQLite
- do not expose dashboards in `/`

Email invitations:

- create an invitation token and optional outbound email
- store only token hashes in SQLite
- become a normal external `user` grant after acceptance
- can be revoked later

## Email Delivery Setup

Invitation delivery is controlled by `DASH_SERVER_EMAIL_PROVIDER`.

Modes:

- `manual`: default. No email is sent. The operator delivers the `accept_url` manually.
- `console`: testing mode. The email is rendered to logs and marked sent.
- `smtp`: generic SMTP or internal relay.
- `ses`
- `sendgrid`
- `postmark`
- `mailgun`
- `resend`

Delivery fields you will see in invitation records:

- `delivery_status`
- `delivery_provider`
- `delivery_message_id`
- `sent_at`
- `delivery_error`

Interpretation:

- `sent`: the provider or relay accepted the message
- `failed`: SMTP submission failed
- `pending_manual_delivery`: no email was sent

## Email Setup Before You Start

Before enabling automatic invitations:

- verify the sender domain with the provider
- decide whether to use authenticated SMTP or a trusted internal relay
- keep credentials in environment variables or a secret manager
- confirm `DASH_SERVER_PUBLIC_BASE_URL` is correct, because invitation links are generated from it
- test with `console` mode first

## Common Email Settings

```bash
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_FROM_NAME="Dash Server"
DASH_SERVER_EMAIL_REPLY_TO="analytics-owners@example.com"
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
```

Use `DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR` rather than embedding secrets directly in config files when possible.

Example:

```bash
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=DASH_SERVER_EMAIL_PASSWORD
DASH_SERVER_EMAIL_PASSWORD="provider-secret-or-api-key"
```

## Generic SMTP Examples

Authenticated SMTP:

```bash
DASH_SERVER_EMAIL_PROVIDER=smtp
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_HOST="smtp.example.com"
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USERNAME="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=DASH_SERVER_EMAIL_PASSWORD
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
```

Trusted internal relay without SMTP auth:

```bash
DASH_SERVER_EMAIL_PROVIDER=smtp
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_HOST="smtp-relay.example.com"
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH=true
```

Use no-auth relay mode only when the relay itself is already protected by network policy, IP allowlists, or another upstream control.

Hosted mode rejects email delivery if both TLS and SSL are disabled.

Recommended transport choices:

- use `587` with STARTTLS in most cases
- use `465` only when the provider explicitly requires implicit SSL/TLS

For implicit SSL/TLS:

```bash
DASH_SERVER_EMAIL_SMTP_USE_SSL=true
DASH_SERVER_EMAIL_SMTP_USE_TLS=false
```

## Provider-Specific Email Examples

Amazon SES:

```bash
DASH_SERVER_EMAIL_PROVIDER=ses
DASH_SERVER_EMAIL_SES_REGION=us-east-1
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_USERNAME="$SES_SMTP_USERNAME"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=SES_SMTP_PASSWORD
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
```

SendGrid:

```bash
DASH_SERVER_EMAIL_PROVIDER=sendgrid
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=SENDGRID_API_KEY
```

Postmark:

```bash
DASH_SERVER_EMAIL_PROVIDER=postmark
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_USERNAME="$POSTMARK_SERVER_TOKEN"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=POSTMARK_SERVER_TOKEN
```

Mailgun:

```bash
DASH_SERVER_EMAIL_PROVIDER=mailgun
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_USERNAME="postmaster@mg.example.com"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=MAILGUN_SMTP_PASSWORD
```

Resend:

```bash
DASH_SERVER_EMAIL_PROVIDER=resend
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=RESEND_API_KEY
```

Microsoft 365 / Exchange Online:

```bash
DASH_SERVER_EMAIL_PROVIDER=smtp
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_HOST="smtp.office365.com"
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USERNAME="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_PASSWORD_ENV_VAR=M365_SMTP_PASSWORD
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
```

Notes:

- the mailbox must allow authenticated SMTP
- `DASH_SERVER_EMAIL_FROM` should normally match the mailbox or an allowed alias
- if your tenant requires OAuth-only SMTP, place an approved relay in front of `dash-server`

Google Workspace SMTP relay:

```bash
DASH_SERVER_EMAIL_PROVIDER=smtp
DASH_SERVER_EMAIL_FROM="dashboards@example.com"
DASH_SERVER_EMAIL_SMTP_HOST="smtp-relay.gmail.com"
DASH_SERVER_EMAIL_SMTP_PORT=587
DASH_SERVER_EMAIL_SMTP_USE_TLS=true
DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH=true
```

Notes:

- allowlist the server egress IP or network in Google Workspace
- configure the relay to accept your sender domain
- do not use consumer Gmail SMTP for hosted invitations

## Recommended Hosted Deployment Sequence

1. Configure hosted baseline settings.
2. Choose `trusted_proxy` or `oidc`.
3. Configure one bootstrap admin principal.
4. Start the server.
5. Verify `/auth/whoami`.
6. Log in as the bootstrap admin.
7. Use `/mcp` to create durable sharing grants.
8. Decide whether public dashboards should remain disabled.
9. Configure email delivery.
10. Test invitation delivery with `console`, then with the real provider.

## Post-Setup Checks

After setup, verify:

- anonymous users cannot open restricted dashboards
- authenticated non-granted users cannot open restricted dashboards
- bootstrap admin can access `/mcp`
- public dashboards are still blocked unless you explicitly enabled public policy
- invitations show the expected delivery status
- accepted invitations reach only the intended dashboard or preview route

## Troubleshooting

- `Hosted mode requires SECRET_KEY or DASH_SERVER_SECRET_KEY.`: set a strong secret.
- `Hosted mode requires SESSION_COOKIE_SECURE=true.`: cookies must be marked secure in hosted mode.
- `Hosted mode requires DASH_SERVER_PUBLIC_BASE_URL to be an https:// URL.`: set the public HTTPS origin.
- `Hosted trusted_proxy auth requires DASH_SERVER_TRUSTED_PROXY_HEADERS_ENABLED=true.`: enable trusted proxy header mode when using `trusted_proxy`.
- `Hosted trusted_proxy auth requires DASH_SERVER_TRUSTED_PROXY_ALLOWED_CIDRS.`: add the proxy source networks.
- `Hosted OIDC auth requires DASH_SERVER_OIDC_ISSUER.`: add the required direct OIDC config, or use `trusted_proxy` instead.
- `/auth/whoami` still shows `anonymous`: the proxy headers are not reaching the app, the source IP is not trusted, or the user has not completed the configured auth flow.
- `/mcp` returns access denied: the principal is anonymous, viewer-only, link-based, or not one of the configured bootstrap/admin/editor/owner users.
- `Email delivery requires DASH_SERVER_EMAIL_FROM.`: set a verified sender address.
- `Email delivery requires DASH_SERVER_EMAIL_SMTP_USERNAME.`: provide SMTP credentials, or set `DASH_SERVER_EMAIL_SMTP_ALLOW_NO_AUTH=true` for a trusted relay.
- `Hosted email delivery requires TLS or SSL for SMTP.`: enable STARTTLS or SSL/TLS.
- `delivery_status=failed`: inspect `delivery_error`; common causes are bad credentials, blocked sender domains, relay restrictions, or TLS mismatch.

## Security Notes

- Never expose `dash-server` directly to the internet while also trusting identity headers from arbitrary clients.
- Keep bootstrap admins minimal and temporary.
- Leave `DASH_SERVER_PUBLIC_DASHBOARDS_ENABLED=false` unless you explicitly want anonymous dashboards.
- Do not commit SMTP credentials, invitation URLs, or raw tokens to Git.
- Treat `/mcp` as privileged administrative access.

## Reference Links

- AWS SES SMTP: https://docs.aws.amazon.com/console/ses/smtp
- AWS SES SMTP credentials: https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html
- SendGrid SMTP: https://www.twilio.com/docs/sendgrid/for-developers/sending-email/integrating-with-the-smtp-api
- SendGrid API keys: https://www.twilio.com/docs/sendgrid/ui/account-and-settings/api-keys/
- Postmark SMTP: https://postmarkapp.com/support/article/811-what-are-the-smtp-details-api-tokens-i-should-be-using
- Mailgun SMTP relay: https://documentation.mailgun.com/docs/mailgun/user-manual/smtp-protocol/smtp-relay
- Mailgun SMTP credentials: https://help.mailgun.com/hc/en-us/articles/203380100-Where-can-I-find-my-API-keys-and-SMTP-credentials
- Resend SMTP: https://resend.com/docs/send-with-smtp
- Microsoft 365 authenticated SMTP submission: https://learn.microsoft.com/en-us/Exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission
- Google Workspace SMTP relay: https://support.google.com/a/answer/2956491
