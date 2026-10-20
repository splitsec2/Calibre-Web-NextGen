# -*- coding: utf-8 -*-
"""Tags page edits and the clean-up map (Discussion #2481, #2438).

A rename, merge or delete on the Tags page is a one-off edit. It becomes a
rule in the value map only when an admin ticks "Also add a rule"
(``add_rule``), so the effect on later books is a visible choice. Every edit
is stored as an applied change set, so the clean-up page's History lists it
and Undo can reverse it: the books first, then any rule it recorded.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import cps.api.browse as browse
import cps.api.value_maps as routes
from cps import db, ub
from tests.unit.value_map_world import app, app_db, library  # noqa: F401

pytestmark = pytest.mark.unit

ADMIN = SimpleNamespace(is_authenticated=True, is_anonymous=False, id=1,
                        role_admin=lambda: True, role_edit=lambda: True)
EDITOR = SimpleNamespace(is_authenticated=True, is_anonymous=False, id=2,
                         role_admin=lambda: False, role_edit=lambda: True)


@pytest.fixture
def page(app, library, app_db):  # noqa: F811
    """``page(route, *args, user=ADMIN, method="POST", json=None, query="")`` -> (status, body)."""

    def run(route, *args, user=ADMIN, method="POST", json=None, query=""):
        with (patch.object(browse.calibre_db, "session", library),
              patch.object(routes.calibre_db, "session", library),
              patch.object(ub, "session", app_db),
              patch.object(browse, "current_user", user),
              patch.object(routes, "current_user", user),
              patch.object(browse, "metadata_db_write_lock"),
              patch.object(routes, "metadata_db_write_lock"),
              patch.object(routes, "_files_rewritten", lambda: True),
              patch.object(browse.helper, "mark_book_modified"),
              patch.object(browse.helper, "log_metadata_change")):
            with app.test_request_context(f"/x{query}", method=method, json=json):
                response = route.__wrapped__(*args)
        if isinstance(response, tuple):
            response, status = response
        else:
            status = response.status_code
        return status, response.get_json()

    return run


def _tag_id(library, name):  # noqa: F811
    return library.query(db.Tags).filter(db.Tags.name == name).one().id


def _rules(app_db):  # noqa: F811
    return {r.incoming: list(r.targets) for r in app_db.query(ub.MetadataValueRule).all()}


def _tags(library, book_id):  # noqa: F811
    library.expire_all()
    return sorted(t.name for t in library.get(db.Books, book_id).tags)


def test_a_rename_adds_no_rule_unless_asked(page, library, app_db):  # noqa: F811
    status, body = page(browse.rename_tag, _tag_id(library, "General"), json={"name": "Generic"})
    assert status == 200 and "rule" not in body
    assert _rules(app_db) == {}
    # Still undoable from the clean-up page: the edit is in History.
    (entry,) = page(routes.value_map_state, "tags", method="GET")[1]["history"]
    assert entry["source"] == "tags_page" and entry["books"] == 1 and entry["rule_changes"] == []


def test_an_admin_rename_with_the_box_ticked_records_the_rule(page, library, app_db):  # noqa: F811
    status, body = page(browse.rename_tag, _tag_id(library, "General"), json={"name": "Generic", "add_rule": True})
    assert status == 200
    assert body["rule"] == {"incoming": "General", "targets": ["Generic"]}
    assert _rules(app_db) == {"General": ["Generic"]}


def test_an_editor_cannot_add_a_rule_but_the_rename_still_happens(page, library, app_db):  # noqa: F811
    status, body = page(browse.rename_tag, _tag_id(library, "General"),
                        user=EDITOR, json={"name": "Generic", "add_rule": True})
    assert status == 200 and "rule" not in body
    assert _rules(app_db) == {}
    assert _tags(library, 1) == ["Fiction / Thrillers", "Generic"]


def test_a_delete_removes_the_tag_from_every_book_and_can_record_a_drop(page, library, app_db):  # noqa: F811
    app_db.add(ub.MetadataValueRule(field="tags", incoming="Genre: General", incoming_key="genre: general",
                                    targets=["General"]))
    app_db.commit()
    status, body = page(browse.delete_tag, _tag_id(library, "General"), method="DELETE", query="?add_rule=1")
    assert status == 200 and body["books"] == 1 and body["rule"] == {"incoming": "General", "targets": []}
    assert _tags(library, 1) == ["Fiction / Thrillers"]
    # The drop is stored, and a rule that produced the deleted tag no longer does.
    assert _rules(app_db) == {"General": [], "Genre: General": []}


def test_a_rename_points_rules_that_produced_the_old_name_at_the_new_one(page, library, app_db):  # noqa: F811
    app_db.add_all([
        ub.MetadataValueRule(field="tags", incoming="Fiction / Thrillers", incoming_key="fiction / thrillers",
                             targets=["Fiction", "thriller"]),
        ub.MetadataValueRule(field="tags", incoming="Thriller", incoming_key="thriller", targets=[]),
    ])
    app_db.commit()
    page(browse.rename_tag, _tag_id(library, "thriller"), json={"name": "Thriller", "add_rule": True})
    rules = _rules(app_db)
    assert rules["Fiction / Thrillers"] == ["Fiction", "Thriller"]
    # A rule that dropped the new name goes: the rename says the new name is wanted.
    assert "Thriller" not in rules or rules["Thriller"] == ["Thriller"]


def test_undo_puts_the_books_back_and_then_the_rule(page, library, app_db):  # noqa: F811
    page(browse.delete_tag, _tag_id(library, "General"), method="DELETE", query="?add_rule=1")
    (entry,) = page(routes.value_map_state, "tags", method="GET")[1]["history"]
    status, result = page(routes.change_set_undo, entry["id"], json={})
    assert status == 200 and result["set"]["status"] == "undone"
    assert _tags(library, 1) == ["Fiction / Thrillers", "General"]
    assert _rules(app_db) == {}


def test_undo_leaves_a_rule_changed_since_and_says_so(page, library, app_db):  # noqa: F811
    page(browse.rename_tag, _tag_id(library, "General"), json={"name": "Generic", "add_rule": True})
    rule = app_db.query(ub.MetadataValueRule).one()
    rule.targets = ["Something else"]
    app_db.commit()
    (entry,) = page(routes.value_map_state, "tags", method="GET")[1]["history"]
    result = page(routes.change_set_undo, entry["id"], json={})[1]
    (change,) = result["set"]["rule_changes"]
    assert change["status"] == "skipped" and change["error"] == "rule changed since"
    assert _rules(app_db) == {"General": ["Something else"]}
    assert _tags(library, 1) == ["Fiction / Thrillers", "General"]


def test_a_page_edit_cannot_be_applied_again(page, library, app_db):  # noqa: F811
    page(browse.rename_tag, _tag_id(library, "General"), json={"name": "Generic"})
    (entry,) = page(routes.value_map_state, "tags", method="GET")[1]["history"]
    assert page(routes.change_set_apply, entry["id"], json={})[0] == 409


def test_a_failure_to_record_never_fails_the_edit(page, library, app_db):  # noqa: F811
    with patch("cps.services.change_sets.record_applied", side_effect=RuntimeError("disk full")):
        status, body = page(browse.rename_tag, _tag_id(library, "General"),
                            json={"name": "Generic", "add_rule": True})
    assert status == 200 and body["name"] == "Generic" and "rule" not in body
    # Rule and history land together or not at all.
    assert _rules(app_db) == {}
    assert app_db.query(ub.MetadataChangeSet).count() == 0
