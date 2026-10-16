# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entity-list browse endpoints for /api/v1."""
from ..unicode_collation import locale_sort_key
from flask import jsonify, request
from flask_babel import gettext as _
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from . import api_v1
from .. import calibre_db, db, helper, logger
from ..cw_login import current_user
from ..services.calibre_db_lock import metadata_db_write_lock
from ..usermanagement import login_required_if_no_ano


@api_v1.route("/authors")
@login_required_if_no_ano
def list_authors():
    rows = (calibre_db.session.query(db.Authors, func.count('books_authors_link.book').label('count'))
            .join(db.books_authors_link)
            .join(db.Books)
            .filter(calibre_db.common_filters())
            .group_by(text('books_authors_link.author'))
            .order_by(locale_sort_key(db.Authors.sort), db.Authors.sort, db.Authors.id)
            .all())
    items = [{"id": a.id, "name": a.name.replace("|", ","), "count": cnt} for a, cnt in rows]
    return {"items": items}


@api_v1.route("/series")
@login_required_if_no_ano
def list_series():
    rows = (calibre_db.session.query(db.Series, func.count('books_series_link.book').label('count'))
            .join(db.books_series_link)
            .join(db.Books)
            .filter(calibre_db.common_filters())
            .group_by(text('books_series_link.series'))
            .order_by(locale_sort_key(db.Series.sort), db.Series.sort, db.Series.id)
            .all())
    items = [{"id": s.id, "name": s.name, "count": cnt} for s, cnt in rows]
    return {"items": items}


@api_v1.route("/tags")
@login_required_if_no_ano
def list_tags():
    rows = (calibre_db.session.query(db.Tags, func.count('books_tags_link.book').label('count'))
            .join(db.books_tags_link)
            .join(db.Books)
            .filter(calibre_db.common_filters())
            .group_by(db.Tags.id)
            .order_by(locale_sort_key(db.Tags.name), db.Tags.name, db.Tags.id)
            .all())
    items = [{"id": t.id, "name": t.name, "count": cnt} for t, cnt in rows]
    return {"items": items}


def _require_metadata_editor():
    """Return an error response when the caller may not edit metadata, else None."""
    if not current_user.is_authenticated or current_user.is_anonymous:
        return jsonify({"error": {"code": "unauthorized", "message": _("You must be signed in")}}), 401
    if not current_user.role_edit():
        return jsonify({"error": {"code": "forbidden", "message": _("You are not allowed to edit metadata")}}), 403
    return None


def _rename_or_merge(model, link_attr, obj_id, name, merge, messages, journal=None):
    """Rename one calibre list value (tag, publisher) for every linked book.

    ``model`` is the calibre table, ``link_attr`` the ``Books`` relationship
    that holds it, ``messages`` the translated texts for this kind of value
    (``not_found``, ``exists``). A name collision is the de-duplication case
    (#973), not merely an error: renaming a value onto its near-duplicate is
    how you consolidate the two. So the 409 carries the conflicting value (id,
    name, how many books it holds) and the caller can repeat the request with
    ``merge: true`` to fold this value into it. The merge stays opt-in and must
    be boolean ``true``: folding two values together destroys the distinction
    between them and there is no undo, so a typo that happens to match an
    existing value must not silently consume it. ``journal``, when given, is
    filled with each affected book's values before and after.
    """
    with metadata_db_write_lock():
        obj = calibre_db.session.get(model, obj_id)
        if obj is None:
            return jsonify({"error": {"code": "not_found", "message": messages["not_found"]}}), 404
        if obj.name == name:
            return jsonify({"id": obj.id, "name": obj.name})

        # Recheck under the process-shared writer lock so two Flask workers
        # cannot both pass the uniqueness check before either commits.
        duplicate = (calibre_db.session.query(model)
                     .filter(func.lower(model.name) == name.lower(), model.id != obj_id)
                     .first())
        if duplicate is not None and not merge:
            # The count is the raw link count, i.e. the true blast radius of the
            # merge this 409 is offering — deliberately not narrowed by
            # common_filters(), which would under-report what actually moves.
            return jsonify({"error": {
                "code": "conflict",
                "message": messages["exists"],
                "conflict": {"id": duplicate.id, "name": duplicate.name, "count": len(duplicate.books)},
            }}), 409

        # Materialize the exact linked-book set while association writers are
        # excluded, then mutate and dirty that same set in one transaction.
        affected_books = list(obj.books)
        before = {b.id: [v.name for v in getattr(b, link_attr)] for b in affected_books}
        try:
            if duplicate is not None:
                # Merge: move every book off this value onto the survivor, whose
                # own spelling wins, then drop the now-empty value. A book already
                # carrying both must end up with one link, not two.
                for book in affected_books:
                    linked = getattr(book, link_attr)
                    if duplicate not in linked:
                        linked.append(duplicate)
                    linked.remove(obj)
                    helper.mark_book_modified(book, set_dirty=True)
                calibre_db.session.delete(obj)
                result = {"id": duplicate.id, "name": duplicate.name,
                          "merged": True, "books": len(affected_books)}
            else:
                obj.name = name
                if hasattr(obj, "sort"):
                    obj.sort = name
                for book in affected_books:
                    helper.mark_book_modified(book, set_dirty=True)
                result = {"id": obj.id, "name": obj.name}
            calibre_db.session.commit()
        except IntegrityError:
            calibre_db.session.rollback()
            return jsonify({"error": {"code": "conflict", "message": messages["exists"]}}), 409
        except Exception:
            calibre_db.session.rollback()
            raise

    # File-level enforcement is best-effort and must only be queued after the
    # database transaction succeeds. Every linked book now carries the surviving
    # name (or, after a merge, no longer carries the one that was folded in).
    for book in affected_books:
        helper.log_metadata_change(
            book, {link_attr: ", ".join(item.name for item in getattr(book, link_attr))})
        if journal is not None:
            journal.append({"book_id": book.id, "field": link_attr, "old": before[book.id],
                            "new": [v.name for v in getattr(book, link_attr)]})
    return jsonify(result)


log = logger.create()


def _record_in_value_map(field, record, journal=(), subject=None):
    """A rename, merge or delete here is also recorded in the value map, so it
    can hold for books that arrive later, and stored as an applied change set
    (the books before and after, and the rules it changed) so the clean-up
    page's History can undo it. Recording is unconditional; whether a rule is
    used (the clean-up page, and later ingest and fetch) is gated where it is
    read, so a stored rule changes nothing by itself. Best-effort: never fails
    the edit. Returns whether the rule was recorded."""
    try:
        from .. import ub
        from ..services import change_sets as cs
        from ..services import value_map as vm
        user_id = getattr(current_user, "id", None)
        rules_before = vm.rules_snapshot(ub.session, field)
        record(vm, ub.session, user_id)
        rule_changes = cs.rule_diff(field, rules_before, vm.rules_snapshot(ub.session, field))
        # The edited value's own rule first; cascades into other rules after it.
        rule_changes.sort(key=lambda rc: vm.fold(rc["incoming"]) != vm.fold(subject or ""))
        if journal or rule_changes:
            cs.record_applied(ub.session, f"{field}_page", user_id, list(journal), rule_changes)
        return True
    except Exception as error:
        log.warning("Value map not updated after a %s edit: %s", field, error)
        return False


def _renamed(response, field, old_name, journal):
    """Record ``old -> new`` once a rename or merge has succeeded, and say so
    in the response (``alias``) so the page can tell the user."""
    if old_name is None or isinstance(response, tuple):
        return response
    data = response.get_json() or {}
    new_name = data.get("name")
    if new_name and new_name != old_name and _record_in_value_map(
            field, lambda vm, s, uid: vm.record_rename(s, field, old_name, new_name, uid), journal, old_name):
        data["alias"] = {"from": old_name, "to": new_name}
        return jsonify(data)
    return response


def _read_new_name(empty_message, type_message, comma_message=None):
    """``(name, merge, None)`` from the request body, or ``(None, None, error)``."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        return None, None, (jsonify({"error": {"code": "invalid_request", "message": type_message}}), 400)
    name = payload["name"].strip()
    if not name:
        return None, None, (jsonify({"error": {"code": "invalid_request", "message": empty_message}}), 400)
    if comma_message and "," in name:
        return None, None, (jsonify({"error": {"code": "invalid_request", "message": comma_message}}), 400)
    return name, payload.get("merge") is True, None


@api_v1.route("/tags/<int:tag_id>", methods=["POST"])
@login_required_if_no_ano
def rename_tag(tag_id):
    """Rename one existing Calibre tag for every linked book (#973).

    See ``_rename_or_merge`` for the merge semantics.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    name, merge, error = _read_new_name(
        _("Tag name cannot be empty"), _("Tag name must be text"), _("Tag name cannot contain commas"))
    if error is not None:
        return error
    tag = calibre_db.session.get(db.Tags, tag_id)
    old_name = tag.name if tag is not None else None
    journal = []
    return _renamed(_rename_or_merge(db.Tags, "tags", tag_id, name, merge, {
        "not_found": _("Tag not found"),
        "exists": _("A tag with that name already exists"),
    }, journal=journal), "tags", old_name, journal)


@api_v1.route("/publishers/<int:publisher_id>", methods=["POST"])
@login_required_if_no_ano
def rename_publisher(publisher_id):
    """Rename one existing Calibre publisher for every linked book; same
    semantics as tags, including the opt-in merge on a name collision."""
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    name, merge, error = _read_new_name(
        _("Publisher name cannot be empty"), _("Publisher name must be text"))
    if error is not None:
        return error
    publisher = calibre_db.session.get(db.Publishers, publisher_id)
    old_name = publisher.name if publisher is not None else None
    journal = []
    return _renamed(_rename_or_merge(db.Publishers, "publishers", publisher_id, name, merge, {
        "not_found": _("Publisher not found"),
        "exists": _("A publisher with that name already exists"),
    }, journal=journal), "publishers", old_name, journal)


@api_v1.route("/tags/<int:tag_id>", methods=["DELETE"])
@login_required_if_no_ano
def delete_tag(tag_id):
    """Remove one Calibre tag from every book that carries it (#973).

    The Tag view offered no way to get rid of a tag at all — not from the list
    and not from the tag's own page — so a mistyped or obsolete tag was
    permanent. Only the tag is deleted; the books it pointed at are untouched
    apart from losing it.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard

    with metadata_db_write_lock():
        tag = calibre_db.session.get(db.Tags, tag_id)
        if tag is None:
            return jsonify({"error": {"code": "not_found", "message": _("Tag not found")}}), 404

        name = tag.name
        affected_books = list(tag.books)
        before = {b.id: [t.name for t in b.tags] for b in affected_books}
        try:
            for book in affected_books:
                book.tags.remove(tag)
                helper.mark_book_modified(book, set_dirty=True)
            calibre_db.session.delete(tag)
            calibre_db.session.commit()
        except Exception:
            calibre_db.session.rollback()
            return jsonify({"error": {"code": "server_error",
                                      "message": _("Tag could not be deleted")}}), 500

    for book in affected_books:
        helper.log_metadata_change(book, {"tags": ", ".join(item.name for item in book.tags)})
    journal = [{"book_id": b.id, "field": "tags", "old": before[b.id], "new": [t.name for t in b.tags]}
               for b in affected_books]
    _record_in_value_map("tags", lambda vm, s, uid: vm.record_delete(s, "tags", name, uid), journal, name)
    return jsonify({"id": tag_id, "name": name, "deleted": True, "books": len(affected_books)})


@api_v1.route("/publishers")
@login_required_if_no_ano
def list_publishers():
    rows = (calibre_db.session.query(db.Publishers, func.count(db.books_publishers_link.c.book).label('count'))
            .join(db.books_publishers_link, db.Publishers.id == db.books_publishers_link.c.publisher)
            .join(db.Books, db.books_publishers_link.c.book == db.Books.id)
            .filter(calibre_db.common_filters())
            .group_by(db.Publishers.id)
            .order_by(locale_sort_key(db.Publishers.name), db.Publishers.name, db.Publishers.id)
            .all())
    items = [{"id": p.id, "name": p.name, "count": cnt} for p, cnt in rows]
    return {"items": items}


@api_v1.route("/languages")
@login_required_if_no_ano
def list_languages():
    lang_list = calibre_db.speaking_language(with_count=True)
    # speaking_language returns [[Category, count], ...] where Category.id = lang_code, Category.name = display name
    items = [{"id": cat.id, "name": cat.name, "count": cnt} for cat, cnt in lang_list]
    return {"items": items}


@api_v1.route("/ratings")
@login_required_if_no_ano
def list_ratings():
    """Browse by star rating. Calibre stores rating as 0-10 (stars*2); the SPA
    filters books by the Ratings row id (matches list_books ?rating=)."""
    rows = (calibre_db.session.query(db.Ratings, func.count('books_ratings_link.book').label('count'))
            .join(db.books_ratings_link)
            .join(db.Books)
            .filter(calibre_db.common_filters())
            .group_by(text('books_ratings_link.rating'))
            .order_by(db.Ratings.rating.desc())
            .all())
    items = [{"id": r.id, "name": "%g★" % (r.rating / 2), "count": cnt} for r, cnt in rows]
    return {"items": items}


@api_v1.route('/personal-ratings')
@login_required_if_no_ano
def list_personal_ratings():
    """Personal score buckets use scores, leaving legacy Calibre row IDs intact."""
    from flask import jsonify
    from ..personal_ratings import personal_score
    from ..sort_orders import viewer_id
    uid = viewer_id(current_user)
    if uid is None:
        return jsonify({'error': {'code': 'unauthorized', 'message': 'You must be signed in'}}), 401
    score = personal_score(uid)
    rows = (calibre_db.session.query(score.label('score'), func.count(db.Books.id))
            .filter(calibre_db.common_filters()).group_by(score).order_by(score.desc()).all())
    response = jsonify({'items': [{'id': value, 'name': '%g★' % (value / 2) if value else _('Unrated'),
                                  'count': count} for value, count in rows]})
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Authorization'
    return response


@api_v1.route("/formats")
@login_required_if_no_ano
def list_formats():
    """Browse by file format (EPUB, PDF, …). The format string is the id; the SPA
    filters books by it (matches list_books ?format=)."""
    rows = (calibre_db.session.query(db.Data.format, func.count(db.Data.book).label('count'))
            .join(db.Books, db.Books.id == db.Data.book)
            .filter(calibre_db.common_filters())
            .group_by(db.Data.format)
            .order_by(db.Data.format)
            .all())
    items = [{"id": fmt, "name": fmt, "count": cnt} for fmt, cnt in rows]
    return {"items": items}
