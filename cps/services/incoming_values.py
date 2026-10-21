# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""The value map applied to incoming metadata (#2438, Discussion #2481).

One stored map, read the same way wherever values come in: a new book's own
values right after import, automatic metadata fetch, and the results of a
manual fetch before they are shown. Each field has its own switch
(``apply_incoming`` in Metadata clean-up), off by default, so an upgrade
changes nothing until an admin turns it on.

Every caller treats a failure here as "leave the values as they came": the
map can make incoming metadata cleaner, it must never stop it arriving.
"""
from . import change_sets as cs
from . import value_map as vm
from .fields import FIELD_SPECS
from .. import logger

log = logger.create()


def _app_session(app_session):
    if app_session is not None:
        return app_session
    from .. import ub
    return ub.session


def map_values(field, values, app_session=None, context=None):
    """``values`` mapped for ``field`` when the field's switch is on, else as given.

    ``context`` names where the values came from ("automatic fetch for book
    12"); each change is then logged with the rule that made it. Without it
    (manual-fetch results, which the user sees) the lines go to debug.
    """
    values = [v for v in (values or []) if v and v.strip()]
    try:
        session = _app_session(app_session)
        if session is None or not vm.apply_incoming(session, field):
            return values
        value_map = vm.load_map(session, field)
        _log_changes(value_map, field, values, context)
        return value_map.map_values(values)
    except Exception as error:
        log.warning("Value map not applied to incoming %s: %s", field, error)
        return values


def _log_changes(value_map, field, values, context):
    lines = value_map.explain(values)
    if not lines:
        return
    if context:
        log.info("Metadata clean-up, %s, %s: %s", context, field, "; ".join(lines))
    else:
        log.debug("Metadata clean-up, fetched %s: %s", field, "; ".join(lines))


def map_one(field, value, app_session=None, context=None):
    """A single-valued field (a publisher): the mapped value, or None when a rule drops it."""
    if not value or not value.strip():
        return value
    mapped = map_values(field, [value.strip()], app_session, context)
    return mapped[0] if mapped else None


def map_search_result(result, app_session=None):
    """A manual-fetch result as the user will see it: tags and publisher mapped."""
    if isinstance(result.get("tags"), list):
        result["tags"] = map_values("tags", result["tags"], app_session)
    if isinstance(result.get("publisher"), str) and result["publisher"].strip():
        result["publisher"] = map_one("publishers", result["publisher"], app_session) or ""
    return result


def map_new_books(calibre_session, app_session, book_ids, *, mark_modified, queue_embed):
    """Apply the map to books just imported, for every field whose switch is on.

    The change is stored as an applied change set (source ``ingest``), so the
    clean-up page's History lists it and Undo can put the file's own values
    back. Values the map left unused are removed, as an apply does. Returns
    the set, or None when nothing changed.
    """
    fields = [f for f in FIELD_SPECS if vm.apply_incoming(app_session, f)]
    if not fields or not book_ids:
        return None
    from .. import db
    maps = {field: vm.load_map(app_session, field) for field in fields}
    changes = []
    for book_id in book_ids:
        book = calibre_session.get(db.Books, book_id)
        if book is None:
            continue
        for field in fields:
            before = cs.current_values(book, field)
            after = maps[field].map_values(before)
            if after != before:
                _log_changes(maps[field], field, before, f"import of book {book_id}")
                changes.append({"book_id": book_id, "field": field, "old": before, "new": after})
    if not changes:
        return None
    change_set = cs.create(app_session, "ingest", None, changes)
    cs.apply(app_session, calibre_session, change_set.id,
             mark_modified=mark_modified, queue_embed=queue_embed)
    return change_set
