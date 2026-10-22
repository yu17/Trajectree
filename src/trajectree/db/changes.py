"""Decode Session-extension changesets into per-row diffs keyed by column name."""

from typing import Any

import apsw

from trajectree.db.schema import table_columns

MaterializedChange = tuple[str, str, list[Any], dict[str, Any] | None, dict[str, Any] | None]


def _row_dict(columns: list[str], values: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    return {name: value for name, value in zip(columns, values) if value is not apsw.no_change}


def materialize(conn: apsw.Connection, changeset: bytes) -> list[MaterializedChange]:
    """Return `(table_name, op, pk, old, new)` for every change in `changeset`.

    `pk` lists the primary-key values in column order, read from `new` for an
    INSERT and from `old` otherwise. `old`/`new` map column name to value and
    leave out columns the changeset carries no value for (`apsw.no_change`).
    INSERT has `old=None`; DELETE has `new=None`.

    Observed shape for an UPDATE (apsw 3.54.1.0): `old` holds the primary-key
    columns plus the previous values of the modified columns, and `new` holds
    only the modified columns, e.g. `old={"id": "p1", "title": "P"}`,
    `new={"title": "Q"}`.
    """
    result: list[MaterializedChange] = []
    for change in apsw.Changeset.iter(changeset):
        columns = table_columns(conn, change.name)
        key_source = change.new if change.op == "INSERT" else change.old
        pk = [key_source[i] for i in sorted(change.pk_columns)]
        result.append(
            (change.name, change.op, pk, _row_dict(columns, change.old), _row_dict(columns, change.new))
        )
    return result
