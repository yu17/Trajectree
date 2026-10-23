# chatmaid contract (v1)

chatmaid is a separate project that handles email and messaging. Trajectree and
chatmaid talk over two small HTTP calls, one in each direction.

## Inbound: chatmaid tells Trajectree about a message

`POST /api/connectors/chatmaid/messages`

Records one `message` signal on the timeline. Trajectree stores a reference and
a short summary only.

### Auth

`Authorization: Bearer <token>`. Accepted: the owner token, or a scoped token
whose `scope.tools` contains `chatmaid`. Cookies are never accepted. A chatmaid
token cannot reach `/mcp` or any other `/api` route.

Mint the token once (owner token required):

```bash
curl -X POST "$TRAJECTREE_URL/api/tokens" \
  -H "Authorization: Bearer $TRAJECTREE_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "chatmaid", "scope": {"projects": [], "tools": ["chatmaid"], "write": false}, "actor_kind": "connector"}'
```

The response carries the `secret` once; give it to chatmaid.

### Request

JSON body, at most 64 KB. Unknown fields are rejected.

| Field | Type | Limits |
| --- | --- | --- |
| `ref` | string | required, 1 to 512 chars; chatmaid's stable id for the message |
| `summary` | string | required, 1 to 2000 chars |
| `channel` | string | required, 1 to 64 chars (`email`, `slack`, ...) |
| `project` | string or null | project slug (or id); default: the `inbox` project |
| `when` | timestamp or null | ISO 8601 with a timezone; default: now |
| `participants` | list of strings | at most 10, each at most 100 chars; default empty |

### Responses

| Status | Meaning |
| --- | --- |
| 200 | `{"signal_id": "...", "project_id": "..."}` |
| 401 | missing, wrong, expired or wrongly scoped token |
| 413 | body larger than 64 KB |
| 422 | invalid fields, an unknown `project`, a `body` field, or no `inbox` project to default to |

### Dedupe

The signal's dedupe key is `chatmaid:<ref>`. Posting the same `ref` again
updates the existing signal (project, time, summary) instead of adding a
second one, so chatmaid may safely resend.

### Never stored

Message bodies, attachments, subjects, addresses. Only `ref`, `summary`,
`channel`, `participants` and the time are kept. A request that includes a
`body` field (or any other unknown field) is refused with 422.

## Outbound: Trajectree delivers a routine result to chatmaid

When a routine's delivery is `chatmaid`, Trajectree sends

`POST {TRAJECTREE_CHATMAID_URL}/deliver`

with `Authorization: Bearer <token>`, where the token is the one stored with

```bash
trajectree connectors login chatmaid
```

(chatmaid issues it; Trajectree keeps it in `<cfg>/connectors/chatmaid_token`, 0600).

JSON payload:

| Field | Type | Meaning |
| --- | --- | --- |
| `title` | string | result title |
| `body_md` | string | result body, Markdown |
| `project` | string or null | project slug, null when the routine has none |
| `run_id` | string | the run that produced it |
| `routine` | string | routine name |

chatmaid answers with any 2xx status when it has accepted the message. The
request times out after 20 seconds.

There are no retries. If `TRAJECTREE_CHATMAID_URL` or the token is not set, the
call fails, the response is not 2xx, or the connection errors, Trajectree falls
back to the `inbox` delivery (a pending note in the inbox) and records
`chatmaid: <status or error>` as the delivery detail. The token is never logged
or placed in a result.

## Versioning

This is v1. Additive changes (new optional fields) keep v1; chatmaid should
ignore payload fields it does not know. Breaking changes will move the paths to
a new version prefix.
