# MCP Client Setup

`dash-server` exposes MCP over Streamable HTTP at:

- `http://127.0.0.1:5100/mcp`

If you start `dash-server` on a different control-plane port, update every MCP
client URL to match. For example, with `DASH_SERVER_PORT=5200` or
`dash-server --port 5200`, the local MCP endpoint is
`http://127.0.0.1:5200/mcp`.

On macOS Monterey 12 and later, AirPlay Receiver can occupy ports `5000` and
`7000`. `dash-server` defaults to `5100` instead of Flask's classic `5000` to
avoid that local macOS collision. If you explicitly use port `5000` and it is
already in use, move the control-plane listener with `DASH_SERVER_PORT` or
`--port`.

This document covers the practical setup paths for ChatGPT and Claude.

## ChatGPT

For local testing, `curl` against `http://127.0.0.1:5100/mcp` is enough.

For ChatGPT itself, you will usually need a reachable HTTPS endpoint, not a localhost address. In practice that means either:

- deploying `dash-server` somewhere reachable
- or tunneling it temporarily with a tool such as `ngrok` or `cloudflared`

### Typical setup flow

1. Start `dash-server`.
2. Expose it on a public HTTPS URL if needed.
3. Open the connector or MCP server settings in ChatGPT for your workspace/account.
4. Add a custom MCP server pointing to:
   `https://your-domain.example.com/mcp`
5. Complete any authentication steps added by your deployment.
6. Start a new chat and use the integration.

### Suggested first prompts

```text
Use the dash-server MCP tools to list hosted apps and summarize their current status.
```

```text
Create a new metric-cards Dash app called support at /apps/support, then tell me when I can open it in the browser.
```

## Claude

Claude supports remote MCP-style integrations. For `dash-server`, there are two main ways to use that:

- connect Claude to a reachable remote `/mcp` endpoint
- or bridge a local HTTP endpoint into Claude Desktop with `mcp-remote`

### Claude Desktop on localhost

If `dash-server` is running locally at `http://127.0.0.1:5100`, you can configure Claude Desktop like this:

```json
{
  "mcpServers": {
    "dash-server": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:5100/mcp"]
    }
  }
}
```

Typical local workflow:

1. Start `dash-server`.
2. Make sure `npx` is available.
3. Add the config above to `claude_desktop_config.json`.
4. Restart Claude Desktop.
5. Start a new conversation and use the `dash-server` integration.

### Remote Claude setup

1. Start `dash-server`.
2. Expose it on a public HTTPS URL if it is only running on localhost.
3. Open Claude or Claude Desktop.
4. Go to the custom connector or remote MCP integration settings.
5. Add a connector that points to:
   `https://your-domain.example.com/mcp`
6. Save it and start a new conversation.

### Suggested first prompts

```text
Use dash-server to list hosted apps, read their health state, and tell me which app is currently live.
```

```text
Create a new Dash app called deals, validate it, and confirm the browser URL where I can open it.
```

## Notes

- `dash-server` currently exposes HTTP MCP. It does not ship a native stdio MCP binary.
- `mcp-remote` is the recommended bridge for local Claude Desktop usage.
- Connector UX changes over time. Use the current OpenAI or Anthropic docs if their setup flow changes.
