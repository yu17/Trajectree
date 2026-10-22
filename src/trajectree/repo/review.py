"""Pending-review lookup over the change log."""

import apsw


def pending_ids(conn: apsw.Connection, table_name: str) -> set[str]:
    """Ids of rows in `table_name` whose most recent change belongs to a pending txn."""
    rows = conn.execute(
        "SELECT json_extract(c.pk, '$[0]') FROM change c"
        " JOIN txn t ON t.id = c.txn_id"
        " WHERE c.table_name = ? AND t.review = 'pending'"
        " AND c.txn_id = (SELECT MAX(c2.txn_id) FROM change c2"
        "                 WHERE c2.table_name = c.table_name AND c2.pk = c.pk)",
        (table_name,),
    )
    return {r[0] for r in rows}
