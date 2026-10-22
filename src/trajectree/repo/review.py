"""Pending-review lookup over the change log."""

import apsw


def pending_txns(conn: apsw.Connection, table_name: str) -> dict[str, str]:
    """Map each row id in `table_name` whose most recent change belongs to a pending txn to that txn id."""
    rows = conn.execute(
        "SELECT json_extract(c.pk, '$[0]'), c.txn_id FROM change c"
        " JOIN txn t ON t.id = c.txn_id"
        " WHERE c.table_name = ? AND t.review = 'pending'"
        " AND c.txn_id = (SELECT MAX(c2.txn_id) FROM change c2"
        "                 WHERE c2.table_name = c.table_name AND c2.pk = c.pk)",
        (table_name,),
    )
    return {row_id: txn_id for row_id, txn_id in rows}
