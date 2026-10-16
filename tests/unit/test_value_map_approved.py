# -*- coding: utf-8 -*-
"""Approved values: the per-field "only approved values" switch, approve and
unapprove, and the Tags page recording renames and deletes into the map.
"""
from types import SimpleNamespace
from unittest.mock import patch

import flask
import pytest
from flask_babel import Babel
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cps import db, ub
import cps.api.browse as browse
from cps.services import change_sets as cs
from cps.services import value_map as vm

pytestmark = pytest.mark.unit


@pytest.fixture
def app_db():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine, tables=[
        ub.MetadataValueRule.__table__, ub.MetadataValueField.__table__, ub.MetadataChangeSet.__table__,
        ub.MetadataChange.__table__, ub.MetadataRuleChange.__table__])
    return sessionmaker(bind=engine)()


def _rules(app_db, field="tags"):
    return {r.incoming: list(r.targets) for r in vm._rules_of(app_db, field)}


def _save(app_db, raw, field="tags"):
    rules, errors = vm.clean_rules(field, raw)
    assert errors == []
    vm.replace_rules(app_db, field, rules, 1)


def test_approved_only_drops_values_no_rule_covers(app_db):
    _save(app_db, {"Suspense": ["Thriller"], "General": []})
    vm.approve(app_db, "tags", ["Fantasy"], 1)
    # Off: unmatched values pass through untouched.
    assert vm.apply_value_map("tags", ["Suspense", "General", "Fantasy", "FIC030000"], app_db) == (
        ["Thriller", "Fantasy", "FIC030000"], [])
    vm.set_approved_only(app_db, "tags", True, 1)
    # A rule's drop is not a rejection; only what nothing covers is.
    assert vm.apply_value_map("tags", ["Suspense", "General", "Fantasy", "FIC030000"], app_db) == (
        ["Thriller", "Fantasy"], ["FIC030000"])


def test_saving_rules_keeps_approvals(app_db):
    vm.approve(app_db, "tags", ["Fantasy", "Horror"], 1)
    _save(app_db, {"Suspense": ["Thriller"]})
    _save(app_db, {"Crime": ["Mystery"]})
    assert _rules(app_db) == {"Fantasy": ["Fantasy"], "Horror": ["Horror"], "Crime": ["Mystery"]}


def test_approve_skips_values_a_rule_already_changes_and_unapprove(app_db):
    _save(app_db, {"Suspense": ["Thriller"]})
    assert vm.approve(app_db, "tags", ["Suspense", "Thriller", "Horror"], 1) == ["Suspense"]
    # Thriller is already approved as a target, so no X -> X row is added for it.
    assert _rules(app_db) == {"Suspense": ["Thriller"], "Horror": ["Horror"]}
    vm.unapprove(app_db, "tags", ["Horror", "Thriller"])
    assert _rules(app_db) == {"Suspense": ["Thriller"]}


def test_rename_records_a_rule_and_retargets_rules_that_produced_the_old_name(app_db):
    _save(app_db, {"Suspense": ["Thrillers"], "Thriller": []})
    vm.approve(app_db, "tags", ["Thrillers"], 1)
    vm.record_rename(app_db, "tags", "Thrillers", "Thriller", 1)
    # Thriller was dropped; the rename says it is wanted, so that drop goes.
    assert _rules(app_db) == {"Suspense": ["Thriller"], "Thrillers": ["Thriller"]}


def test_delete_records_a_drop_and_rules_stop_producing_it(app_db):
    _save(app_db, {"Action & Adventure": ["Action", "Adventure"], "Suspense": ["Thriller"]})
    vm.approve(app_db, "tags", ["Thriller"], 1)
    vm.record_delete(app_db, "tags", "Thriller", 1)
    assert _rules(app_db) == {"Action & Adventure": ["Action", "Adventure"], "Suspense": [], "Thriller": []}


# --- The Tags page -----------------------------------------------------------

@pytest.fixture
def tag_session():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect",
                 lambda connection, _record: connection.execute("ATTACH DATABASE ':memory:' AS calibre"))
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    thrillers, general = db.Tags("Thrillers"), db.Tags("General")
    session.add_all([thrillers, general, db.Publishers("Zebra Books", "Zebra Books")])
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    book = db.Books("Book 1", "Book 1", "Author", now, now, "1.0", now, "b1", 1, [], [])
    book.tags.extend([thrillers, general])
    session.add(book)
    session.commit()
    yield session
    session.close()


def _call(route, tag_session, app_db, tag_id, method="POST", payload=None):
    app = flask.Flask(__name__)
    Babel(app)
    editor = SimpleNamespace(is_authenticated=True, is_anonymous=False, id=1, role_edit=lambda: True)
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", editor),
          patch.object(browse, "metadata_db_write_lock"),
          patch.object(browse.helper, "log_metadata_change"),
          patch.object(ub, "session", app_db)):
        with app.test_request_context(json=payload, method=method):
            return route.__wrapped__(tag_id)


def _tag_id(session, name):
    return session.query(db.Tags).filter_by(name=name).one().id


@pytest.mark.parametrize("approved_only", [False, True])
def test_tags_page_rename_and_delete_always_record_rules(tag_session, app_db, approved_only):
    # Recording does not depend on the switch; the switch gates use, not recording.
    vm.set_approved_only(app_db, "tags", approved_only, 1)
    response = _call(browse.rename_tag, tag_session, app_db, _tag_id(tag_session, "Thrillers"),
                     payload={"name": "Thriller"})
    assert response.get_json()["alias"] == {"from": "Thrillers", "to": "Thriller"}
    _call(browse.delete_tag, tag_session, app_db, _tag_id(tag_session, "General"), method="DELETE")
    assert _rules(app_db) == {"Thrillers": ["Thriller"], "General": []}


def test_a_failed_recording_does_not_fail_the_rename(tag_session, app_db, monkeypatch):
    monkeypatch.setattr(vm, "record_rename", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("app.db locked")))
    response = _call(browse.rename_tag, tag_session, app_db, _tag_id(tag_session, "Thrillers"),
                     payload={"name": "Thriller"})
    assert response.get_json() == {"id": response.get_json()["id"], "name": "Thriller"}


def test_publishers_page_rename_records_a_rule(tag_session, app_db):
    publisher_id = tag_session.query(db.Publishers).filter_by(name="Zebra Books").one().id
    response = _call(browse.rename_publisher, tag_session, app_db, publisher_id, payload={"name": "Zebra"})
    assert response.get_json()["alias"] == {"from": "Zebra Books", "to": "Zebra"}
    assert _rules(app_db, "publishers") == {"Zebra Books": ["Zebra"]}


def _book_tags(session):
    session.expire_all()
    return sorted(t.name for t in session.query(db.Books).one().tags)


def _undo(tag_session, app_db, set_id):
    return cs.undo(app_db, tag_session, set_id, mark_modified=lambda b: None, queue_embed=lambda b, c: None)


def test_a_tags_page_rename_is_in_history_and_undo_puts_books_and_rules_back(tag_session, app_db):
    _call(browse.rename_tag, tag_session, app_db, _tag_id(tag_session, "Thrillers"), payload={"name": "Thriller"})
    assert _book_tags(tag_session) == ["General", "Thriller"]
    change_set = app_db.query(ub.MetadataChangeSet).one()
    summary = cs.summary(app_db, tag_session, change_set)
    assert (summary["source"], summary["status"]) == ("tags_page", "applied")
    assert [(c["before"], c["after"]) for c in summary["changes"]] == [(["General", "Thrillers"], ["General", "Thriller"])]
    assert summary["rule_changes"][0]["incoming"] == "Thrillers"

    _undo(tag_session, app_db, change_set.id)
    assert _book_tags(tag_session) == ["General", "Thrillers"]
    assert _rules(app_db) == {}


def test_a_tags_page_delete_undoes_too(tag_session, app_db):
    _call(browse.delete_tag, tag_session, app_db, _tag_id(tag_session, "General"), method="DELETE")
    assert _book_tags(tag_session) == ["Thrillers"] and _rules(app_db) == {"General": []}
    _undo(tag_session, app_db, app_db.query(ub.MetadataChangeSet).one().id)
    assert _book_tags(tag_session) == ["General", "Thrillers"] and _rules(app_db) == {}


def test_undo_leaves_a_rule_changed_since_and_says_so(tag_session, app_db):
    _call(browse.rename_tag, tag_session, app_db, _tag_id(tag_session, "Thrillers"), payload={"name": "Thriller"})
    vm.replace_rules(app_db, "tags", vm.clean_rules("tags", {"Thrillers": ["Suspense"]})[0], 1)
    set_id = app_db.query(ub.MetadataChangeSet).one().id
    _undo(tag_session, app_db, set_id)
    assert _rules(app_db) == {"Thrillers": ["Suspense"]}
    assert [(r.status, r.error) for r in cs.rule_rows(app_db, set_id)] == [("skipped", "rule changed since")]
