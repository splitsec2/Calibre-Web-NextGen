# SPDX-License-Identifier: GPL-3.0-or-later
"""Publisher rename and merge: the tag route's ``_rename_or_merge`` for publishers.

Same semantics as #973 for tags: a collision answers 409 with the conflicting
publisher, ``merge: true`` folds one into the other, a plain rename keeps the
row (and its sort) in step.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cps import db
import cps.api.browse as browse
from tests.unit.value_map_world import app  # noqa: F401  (the shared Flask app, #2546)

pytestmark = pytest.mark.unit


def _book(index, publishers):
    now = datetime.now(timezone.utc)
    book = db.Books(f"Book {index}", f"Book {index}", "Author", now, now,
                    "1.0", now, f"book-{index}", 1, [], [])
    for publisher in publishers:
        book.publishers.append(publisher)
    return book


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect",
                 lambda connection, _record: connection.execute("ATTACH DATABASE ':memory:' AS calibre"))
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    variant = db.Publishers("Zebra Books", "Zebra Books")
    canonical = db.Publishers("Zebra", "Zebra")
    other = db.Publishers("Kensington Publishing Corp.", "Kensington Publishing Corp.")
    session.add_all([variant, canonical, other,
                     _book(1, [variant]), _book(2, [variant]), _book(3, [canonical]), _book(4, [other])])
    session.commit()
    yield session
    session.close()


def _editor():
    return type("Editor", (), {
        "is_authenticated": True, "is_anonymous": False, "role_edit": lambda self: True})()


def _publisher(session, name):
    return session.query(db.Publishers).filter_by(name=name).one()


def _call(session, app, publisher_id, payload):
    with (patch.object(browse.calibre_db, "session", session),
          patch.object(browse, "current_user", _editor()),
          patch.object(browse, "metadata_db_write_lock"),
          patch.object(browse.helper, "log_metadata_change") as logged):
        with app.test_request_context(json=payload, method="POST"):
            response = browse.rename_publisher.__wrapped__(publisher_id)
    return response, logged


def test_collision_reports_the_conflicting_publisher(session, app):
    variant = _publisher(session, "Zebra Books")
    (body, status), _ = _call(session, app, variant.id, {"name": "Zebra"})
    assert status == 409
    conflict = body.get_json()["error"]["conflict"]
    assert conflict["name"] == "Zebra" and conflict["count"] == 1
    assert _publisher(session, "Zebra Books")  # nothing moved


def test_merge_moves_every_book_and_drops_the_variant(session, app):
    variant = _publisher(session, "Zebra Books")
    response, logged = _call(session, app, variant.id, {"name": "Zebra", "merge": True})
    assert response.get_json()["merged"] is True and response.get_json()["books"] == 2
    session.expire_all()
    assert session.query(db.Publishers).filter_by(name="Zebra Books").count() == 0
    assert len(_publisher(session, "Zebra").books) == 3
    assert logged.call_count == 2


def test_plain_rename_keeps_the_sort_in_step(session, app):
    other = _publisher(session, "Kensington Publishing Corp.")
    response, _ = _call(session, app, other.id, {"name": "Kensington"})
    assert response.get_json() == {"id": other.id, "name": "Kensington"}
    session.expire_all()
    assert _publisher(session, "Kensington").sort == "Kensington"
