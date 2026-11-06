# Email gateway

IMAP mailbox → OpenHands conversations. A message from an allowed sender is
routed to a workspace, a conversation is started with its instructions, an
acknowledgement is emailed back, and the final answer is emailed when the
conversation finishes. No LLM is involved in polling, routing or replies.

## How it runs

In production it runs as an OpenHands **cron automation** (every 2 minutes,
Europe/Brussels), declared by [`.plugin/plugin.json`](.plugin/plugin.json) and
executed by [`automation_entry.py`](automation_entry.py). The automation
runtime provides:

- `AGENT_SERVER_URL` + `SESSION_API_KEY` to call the agent server API;
- `EMAIL_PASSWORD` through `/api/settings/secrets/EMAIL_PASSWORD`;
- `AUTOMATION_API_URL` + `AUTOMATION_KV_TOKEN` for state that survives runs.

Locally, the same code can be run by hand:

```bash
EMAIL_PASSWORD=... python3 gateway.py poll        # one cycle
EMAIL_PASSWORD=... python3 gateway.py poll --dry-run --limit 1
EMAIL_PASSWORD=... python3 gateway.py selftest
```

`watch` keeps polling in the foreground (useful while debugging); it is not
needed when the cron automation is active.

## Configuration

`config.json` holds the mailbox, allow-list, workspace routing and reply
settings. `config.example.json` is a sanitized template. The password is never
stored in the config: it comes from `EMAIL_PASSWORD` (or
`EMAIL_IMAP_PASSWORD` / `EMAIL_SMTP_PASSWORD`), falling back to the agent
server secret store when running as an automation.

Senders are rejected unless their address is in `security.allowed_senders` or
their domain is in `security.allowed_domains` (or the subject carries
`security.subject_prefix`). Replies from the gateway itself are ignored.
Personal addresses can be supplied through `EMAIL_ALLOWED_SENDERS`
(comma-separated, environment variable or agent secret) so they stay out of
the committed config.

## Routing

A message can name its target explicitly:

```
Projet: Co-Living Hub
Branche: main
```

or in the subject: `[projet:PBDR Homes]`. Otherwise workspace names and aliases
found in the message body are matched, and the default workspace
(`routing.default_workspace`) is used when nothing matches.

The raw message is archived as `.eml` under the `Email` workspace and, when
`routing.archive_inbox_conversations` is true, an inbox record conversation is
created there. That record never runs an agent: it contains a single redirect
message pointing to the working conversation(s), so work happens exactly once.

## Emails

Acknowledgements and completion emails are sent as `multipart/alternative`
with both a plain-text and an HTML part. The HTML part is rendered from the
agent's Markdown answer (headings, lists, code blocks, tables, links) with
inline styles so it displays correctly in common mail clients.

## Replying

Acknowledgements and completion emails contain `[conv:xxxxxxxx]`. Replying with
that marker in the subject adds the message to the same conversation. In-Reply-To
and References headers also resume the thread.

## State

`email-gateway-state` in the automation KV store tracks processed messages,
conversation ids, threads and notification flags. A local `.state.json` is kept
as a fallback/debug copy.
