Parse the owner's note into proposals for the project tracker. The note was written on $today.

## Note

$text

## Projects

$projects

## Rules

- Produce zero or more proposals. Use an event for something that happened or is planned, and a todo for something owed.
- Write dates as ISO 8601 with a `Z` suffix, resolving relative dates ("Friday", "tomorrow") against $today.
- Set `project` to one slug from the list above; when unclear, use `$default_project`.
- Give every proposal a short `reason` quoting or paraphrasing the part of the note it came from.
- Do not invent facts that are not in the note.
- Make `answer` one sentence saying what you understood.
