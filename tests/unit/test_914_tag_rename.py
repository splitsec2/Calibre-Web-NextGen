# SPDX-License-Identifier: GPL-3.0-or-later
"""#914: editors can safely rename a tag globally from its New-UI entity page."""
from datetime import datetime, timezone
from unittest.mock import call, patch

import flask
import pytest
from flask_babel import Babel
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cps import db
import cps.api.browse as browse


@pytest.fixture(autouse=True)
def _no_value_map_recording(monkeypatch):
    # The rename and delete routes also record into the clean-up value map and then
    # answer with an "alias"; that has its own tests (test_value_map_approved.py).
    # These cover the rename itself, whatever state an earlier test left app.db in.
    monkeypatch.setattr(browse, "_record_in_value_map", lambda *args, **kwargs: False)


@pytest.fixture()
def tag_session():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda connection, _record: connection.execute("ATTACH DATABASE ':memory:' AS calibre"))
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    old = db.Tags("Old name")
    now = datetime.now(timezone.utc)
    books = [
        db.Books(f"Linked book {index}", f"Linked book {index}", "Author", now, now,
                 "1.0", now, f"linked-book-{index}", 1, [], [])
        for index in (1, 2)
    ]
    for book in books:
        book.tags.append(old)
    session.add_all([old, db.Tags("Existing"), *books])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def app():
    app = flask.Flask(__name__)
    Babel(app)
    return app


def _editor():
    return type("Editor", (), {
        "is_authenticated": True,
        "is_anonymous": False,
        "role_edit": lambda self: True,
    })()


def _viewer():
    return type("Viewer", (), {
        "is_authenticated": True,
        "is_anonymous": False,
        "role_edit": lambda self: False,
    })()


def _anonymous():
    return type("Anonymous", (), {
        "is_authenticated": False,
        "is_anonymous": True,
        "role_edit": lambda self: False,
    })()


@pytest.mark.unit
def test_tag_rename_persists_and_rejects_empty_or_duplicate_names(tag_session, app):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    linked_books = list(old.books)
    previous_modified = {book.id: book.last_modified for book in linked_books}
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", _editor()),
          patch.object(browse, "metadata_db_write_lock") as write_lock,
          patch.object(browse.helper, "log_metadata_change") as log_change):
        with app.test_request_context(json={"name": "  Better name  "}, method="POST"):
            response = browse.rename_tag.__wrapped__(old.id)
            assert response.get_json() == {"id": old.id, "name": "Better name"}
        assert tag_session.get(db.Tags, old.id).name == "Better name"
        for book in linked_books:
            assert book.last_modified >= previous_modified[book.id]
            assert tag_session.query(db.Metadata_Dirtied).filter_by(book=book.id).one()
        assert log_change.call_count == 2
        log_change.assert_has_calls(
            [call(book, {"tags": "Better name"}) for book in linked_books], any_order=True)
        write_lock.return_value.__enter__.assert_called_once()
        write_lock.return_value.__exit__.assert_called_once()

        with app.test_request_context(json={"name": "  "}, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == 400
        with app.test_request_context(json={"name": "existing"}, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == 409
        assert tag_session.get(db.Tags, old.id).name == "Better name"


@pytest.mark.unit
@pytest.mark.parametrize("payload", [[], {"name": 123}, {"name": {"nested": "object"}}, {"name": "one,two"}])
def test_tag_rename_rejects_malformed_or_unrepresentable_names(tag_session, app, payload):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", _editor()),
          patch.object(browse, "metadata_db_write_lock")):
        with app.test_request_context(json=payload, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == 400
    assert tag_session.get(db.Tags, old.id).name == "Old name"


@pytest.mark.unit
def test_tag_rename_rolls_back_a_commit_time_duplicate_race(tag_session, app):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    real_rollback = tag_session.rollback
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", _editor()),
          patch.object(tag_session, "commit", side_effect=IntegrityError("unique", {}, None)),
          patch.object(tag_session, "rollback", wraps=real_rollback) as rollback,
          patch.object(browse, "metadata_db_write_lock"),
          patch.object(browse.helper, "log_metadata_change") as log_change):
        with app.test_request_context(json={"name": "Racing name"}, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == 409
        rollback.assert_called_once()
        log_change.assert_not_called()
    assert tag_session.get(db.Tags, old.id).name == "Old name"
    assert tag_session.query(db.Tags).count() == 2


@pytest.mark.unit
def test_tag_rename_rolls_back_an_autoflush_integrity_race(tag_session, app):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    real_rollback = tag_session.rollback
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", _editor()),
          patch.object(browse, "metadata_db_write_lock"),
          patch.object(browse.helper, "mark_book_modified",
                       side_effect=IntegrityError("autoflush unique", {}, None)),
          patch.object(tag_session, "rollback", wraps=real_rollback) as rollback,
          patch.object(browse.helper, "log_metadata_change") as log_change):
        with app.test_request_context(json={"name": "Racing name"}, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == 409
        rollback.assert_called_once()
        log_change.assert_not_called()
    assert tag_session.get(db.Tags, old.id).name == "Old name"


@pytest.mark.unit
def test_tag_rename_same_name_is_a_side_effect_free_noop(tag_session, app):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    before = {book.id: book.last_modified for book in old.books}
    with (patch.object(browse.calibre_db, "session", tag_session),
          patch.object(browse, "current_user", _editor()),
          patch.object(browse, "metadata_db_write_lock"),
          patch.object(browse.helper, "mark_book_modified") as mark_modified,
          patch.object(browse.helper, "log_metadata_change") as log_change):
        with app.test_request_context(json={"name": "  Old name  "}, method="POST"):
            response = browse.rename_tag.__wrapped__(old.id)
            assert response.get_json() == {"id": old.id, "name": "Old name"}
        mark_modified.assert_not_called()
        log_change.assert_not_called()
    assert tag_session.query(db.Metadata_Dirtied).count() == 0
    assert {book.id: book.last_modified for book in old.books} == before


@pytest.mark.unit
@pytest.mark.parametrize(
    ("user_factory", "expected_status"),
    [(_anonymous, 401), (_viewer, 403)],
)
def test_tag_rename_rejects_users_without_edit_permission(tag_session, app, user_factory, expected_status):
    old = tag_session.query(db.Tags).filter_by(name="Old name").one()
    with patch.object(browse.calibre_db, "session", tag_session), patch.object(browse, "current_user", user_factory()):
        with app.test_request_context(json={"name": "Forbidden rename"}, method="POST"):
            assert browse.rename_tag.__wrapped__(old.id)[1] == expected_status
    assert tag_session.get(db.Tags, old.id).name == "Old name"
