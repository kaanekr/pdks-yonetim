"""Update keyed Tk rows in place without disturbing selection, focus or viewport."""
import json


def row_key(*parts):
    return json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def sync_rows(tree, rows):
    """Rows are (stable iid, values). Only missing identities are removed."""
    rows = [(str(key), tuple(str(value) for value in values)) for key, values in rows]
    old = tree.get_children()
    wanted = {key for key, _values in rows}
    xview, yview = tree.xview(), tree.yview()
    removed = [key for key in old if key not in wanted]
    if removed:
        tree.delete(*removed)
    for key, values in rows:
        if tree.exists(key):
            if tuple(str(value) for value in tree.item(key, 'values')) != values:
                tree.item(key, values=values)
        else:
            tree.insert('', 'end', iid=key, values=values)
    order = tuple(key for key, _values in rows)
    if tree.get_children() != order:
        for index, key in enumerate(order):
            tree.move(key, '', index)
    if xview:
        tree.xview_moveto(xview[0])
    if yview:
        tree.yview_moveto(yview[0])
