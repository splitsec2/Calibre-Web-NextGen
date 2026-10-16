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
from cps.services import value_map as vm

pytestmark = pytest.mark.unit


@pytest.fixture
def app_db():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine, tables=[
        ub.MetadataValueRule.__table__, ub.MetadataValueField.__table__])
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
    session.add_all([db.Tags("Thrillers"), db.Tags("General")])
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
