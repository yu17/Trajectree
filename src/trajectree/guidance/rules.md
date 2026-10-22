<!-- managed by trajectree init-project; edits are overwritten -->
# Trajectree

This folder is Trajectree project `{slug}` ({title}). The `trajectree` MCP server records the history of this project: decisions, milestones, blockers and follow-ups.

## When to record

Record an entry as soon as one of these happens in this session:

- a decision is taken
- a milestone is reached
- a blocker is found
- a follow-up is owed
- a relation to another project is noticed

## How

- Call `record_event` with a `kind` of decision, milestone, blocker, meeting, idea or note, a short `title`, and an `intent`: one line saying why you are recording it. The `intent` is stored in the ledger.
- Call `add_todo` for each follow-up that is owed.
- Call `link` to relate two items, with a `rel` such as `depends_on`, `derived_from`, `caused` or `relates_to`.
- Omit `project`; the server defaults it from this folder.

## Style

- Keep the title to 80 characters or fewer and the body to 5 lines or fewer.
- Never include secrets.
- Never paste raw logs or tool output.
- Write one entry per outcome.
- Call `search` before recording something that may already exist.

## Review

Every write lands as pending. The owner accepts or rejects it in the Trajectree inbox, so do not retry or re-record a write that is waiting for review.

## Context

- Call `get_context_pack` when the owner asks about other projects or how this work relates to them.
- Call `get_project` for this project's summary and open todos.
