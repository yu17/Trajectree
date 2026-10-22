"""Row helpers shared by the repository modules."""

import json
from typing import Any, TypeVar

import apsw
from pydantic import BaseModel

from trajectree.db.schema import table_columns

T = TypeVar("T", bound=BaseModel)


def dumps_props(d: dict[str, Any]) -> str:
    return json.dumps(d)


def row_to_model(conn: apsw.Connection, table: str, row: tuple[Any, ...], model: type[T]) -> T:
    data = dict(zip(table_columns(conn, table), row))
    if isinstance(data.get("props"), str):
        data["props"] = json.loads(data["props"])
    return model.model_validate(data)


def select_row(conn: apsw.Connection, table: str, row_id: str) -> tuple[Any, ...] | None:
    """Fetch one row by id; `table` must be a trusted literal, never user input."""
    cols = ", ".join(table_columns(conn, table))
    return conn.execute(f"SELECT {cols} FROM {table} WHERE id = ?", (row_id,)).fetchone()


def select_rows(
    conn: apsw.Connection, table: str, where: list[str], params: list[Any], order_by: str
) -> list[tuple[Any, ...]]:
    """Select rows; `where` and `order_by` are trusted SQL fragments, values go in `params`."""
    cols = ", ".join(table_columns(conn, table))
    sql = f"SELECT {cols} FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order_by}"
    return list(conn.execute(sql, params))


def update_fields(conn: apsw.Connection, table: str, row_id: str, fields: dict[str, Any]) -> None:
    """UPDATE the given columns; keys are checked against the table's columns."""
    unknown = set(fields) - set(table_columns(conn, table))
    if unknown:
        raise ValueError(f"unknown columns: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*fields.values(), row_id])
