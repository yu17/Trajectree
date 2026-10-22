from trajectree.ids import new_id


def test_new_id_is_26_char_ulid():
    assert len(new_id()) == 26


def test_new_ids_sort_in_creation_order():
    a = new_id(); b = new_id()
    assert a < b
