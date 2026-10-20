# -*- coding: utf-8 -*-
"""Publishers page edits and the clean-up map: the Tags page behaviour for
publishers (rename and merge, no delete: a book has one publisher)."""
import pytest

import cps.api.browse as browse
import cps.api.value_maps as routes
from cps import db, ub
from tests.unit.test_tags_page_rules import ADMIN, EDITOR, page  # noqa: F401
from tests.unit.value_map_world import app, app_db, library  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def publishers(library):  # noqa: F811
    """Book 1: "Penguin Books Ltd"; book 2: "Penguin"."""
    library.execute(db.Publishers.__table__.insert(), [
        {"id": 1, "name": "Penguin Books Ltd", "sort": "Penguin Books Ltd"},
        {"id": 2, "name": "Penguin", "sort": "Penguin"},
    ])
    library.execute(db.books_publishers_link.insert(), [{"book": 1, "publisher": 1}, {"book": 2, "publisher": 2}])
    library.commit()
    return library


def _rules(app_db):  # noqa: F811
    return {r.incoming: list(r.targets) for r in
            app_db.query(ub.MetadataValueRule).filter(ub.MetadataValueRule.field == "publishers").all()}


def test_a_merge_with_the_box_ticked_records_the_publisher_rule(page, publishers, app_db):  # noqa: F811
    status, body = page(browse.rename_publisher, 1, json={"name": "Penguin", "merge": True, "add_rule": True})
    assert status == 200 and body["merged"] is True
    assert body["rule"] == {"incoming": "Penguin Books Ltd", "targets": ["Penguin"]}
    assert _rules(app_db) == {"Penguin Books Ltd": ["Penguin"]}
    (entry,) = page(routes.value_map_state, "publishers", method="GET")[1]["history"]
    assert entry["source"] == "publishers_page" and entry["books"] == 1


def test_an_editor_renames_a_publisher_without_a_rule(page, publishers, app_db):  # noqa: F811
    status, body = page(browse.rename_publisher, 1, user=EDITOR, json={"name": "Penguin UK", "add_rule": True})
    assert status == 200 and "rule" not in body
    assert _rules(app_db) == {}


def test_undo_of_a_publisher_merge_restores_the_book_and_the_rule(page, publishers, app_db):  # noqa: F811
    page(browse.rename_publisher, 1, json={"name": "Penguin", "merge": True, "add_rule": True})
    (entry,) = page(routes.value_map_state, "publishers", method="GET")[1]["history"]
    assert page(routes.change_set_undo, entry["id"], json={})[0] == 200
    publishers.expire_all()
    assert [p.name for p in publishers.get(db.Books, 1).publishers] == ["Penguin Books Ltd"]
    assert _rules(app_db) == {}
