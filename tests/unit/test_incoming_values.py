# -*- coding: utf-8 -*-
"""The clean-up map applied to incoming metadata (#2438, Discussion #2481).

Per field, off by default: a new book's own values right after import,
automatic fetch, and manual-fetch results, all read the same stored rules.
Off, or on any failure, values arrive exactly as they came.
"""
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text

import cps.api.value_maps as routes
import cps.metadata_helper as m
from cps import db, ub
from cps.services import incoming_values as iv
from cps.services import value_map as vm
from tests.unit.test_metadata_autofetch_safety import _Book, _FakeCDB, _meta, _patch_settings
from tests.unit.value_map_world import app, app_db, call, library  # noqa: F401

pytestmark = pytest.mark.unit


def _rules(app_db, raw, field="tags", on=True):  # noqa: F811
    rules, errors = vm.clean_rules(field, raw)
    assert errors == []
    vm.replace_rules(app_db, field, rules, 1)
    vm.set_field_settings(app_db, field, 1, apply_incoming=on)


def _names(library, book_id, attr="tags"):  # noqa: F811
    library.expire_all()
    return [row.name for row in getattr(library.get(db.Books, book_id), attr)]


def test_off_by_default_values_arrive_as_they_came(app_db):  # noqa: F811
    rules, _ = vm.clean_rules("tags", {"General": []})
    vm.replace_rules(app_db, "tags", rules, 1)
    assert iv.map_values("tags", ["General", "Dragons"], app_db) == ["General", "Dragons"]


def test_on_values_are_mapped_and_a_dropped_publisher_is_none(app_db):  # noqa: F811
    _rules(app_db, {"Fiction / Thrillers": ["Fiction", "Thriller"], "General": []})
    assert iv.map_values("tags", ["Fiction / Thrillers", "General", "thriller"], app_db) == ["Fiction", "Thriller"]
    _rules(app_db, {"Penguin Books Ltd": ["Penguin"], "Unknown": []}, field="publishers")
    assert iv.map_one("publishers", "Penguin Books Ltd", app_db) == "Penguin"
    assert iv.map_one("publishers", "Unknown", app_db) is None


def test_a_broken_map_never_stops_the_values():
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("no such table")
    assert iv.map_values("tags", ["General"], broken) == ["General"]


def test_manual_fetch_results_are_shown_mapped(app_db):  # noqa: F811
    _rules(app_db, {"FIC030000": [], "Fiction / Thrillers": ["Thriller"]})
    _rules(app_db, {"Penguin Books Ltd": ["Penguin"]}, field="publishers")
    result = iv.map_search_result({"tags": ["FIC030000", "Fiction / Thrillers"], "publisher": "Penguin Books Ltd"},
                                  app_db)
    assert result == {"tags": ["Thriller"], "publisher": "Penguin"}


def test_import_maps_a_new_book_and_history_can_undo_it(call, library, app_db):  # noqa: F811
    _rules(app_db, {"Fiction / Thrillers": ["Thriller"], "General": []})
    queued = []
    change_set = iv.map_new_books(library, app_db, [1, 3], mark_modified=lambda book: None,
                                  queue_embed=lambda book, changed: queued.append(book.id))
    assert change_set.source == "ingest" and change_set.status == "applied"
    assert _names(library, 1) == ["Thriller"] and _names(library, 3) == ["Dragons"]
    assert queued == [1]
    # The file's own values no book uses any more are gone, as after an apply.
    assert library.query(db.Tags).filter(db.Tags.name == "General").count() == 0
    (entry,) = call(routes.value_map_state, "tags")[1]["history"]
    assert entry["source"] == "ingest"
    assert call(routes.change_set_undo, entry["id"], method="POST", json={})[0] == 200
    assert sorted(_names(library, 1)) == ["Fiction / Thrillers", "General"]


def test_import_with_the_switch_off_changes_nothing(library, app_db):  # noqa: F811
    _rules(app_db, {"General": []}, on=False)
    assert iv.map_new_books(library, app_db, [1], mark_modified=lambda b: None, queue_embed=lambda b, c: None) is None
    assert sorted(_names(library, 1)) == ["Fiction / Thrillers", "General"]


def test_automatic_fetch_applies_the_map_to_what_it_brings(monkeypatch, app_db):  # noqa: F811
    _patch_settings(monkeypatch)
    _rules(app_db, {"FIC030000": [], "Fiction / Thrillers": ["Thriller"]})
    _rules(app_db, {"Penguin Books Ltd": ["Penguin"]}, field="publishers")
    book = _Book()
    with patch.object(ub, "session", app_db):
        m._apply_metadata_to_book(book, _meta(tags=["FIC030000", "Fiction / Thrillers"],
                                              publisher="Penguin Books Ltd"), _FakeCDB())
    assert [t.name for t in book.tags] == ["Thriller"]
    assert [p.name for p in book.publishers] == ["Penguin"]


def test_the_settings_route_sets_one_switch_and_keeps_the_other(call):
    call(routes.value_map_settings, "tags", method="PUT", json={"approved_only": True})
    status, state = call(routes.value_map_settings, "tags", method="PUT", json={"apply_incoming": True})
    assert status == 200 and state["apply_incoming"] is True and state["approved_only"] is True
    assert call(routes.value_map_settings, "tags", method="PUT", json={"apply_incoming": "on"})[0] == 400
    assert call(routes.value_map_settings, "tags", method="PUT", json={})[0] == 400


def test_an_app_db_from_before_the_switch_gains_the_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE metadata_value_field (field VARCHAR(32) PRIMARY KEY, "
                                "approved_only BOOLEAN NOT NULL, updated_by INTEGER, updated_at DATETIME)"))
        connection.execute(text("INSERT INTO metadata_value_field (field, approved_only) VALUES ('tags', 1)"))
    ub.migrate_metadata_value_rule_table(engine, None)
    ub.migrate_metadata_value_rule_table(engine, None)   # twice: the second run must be a no-op
    with engine.connect() as connection:
        row = connection.execute(text("SELECT approved_only, apply_incoming FROM metadata_value_field")).one()
    assert tuple(row) == (1, 0)


def _processor_module(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    monkeypatch.setenv("CWA_METADATA_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("CALIBRE_DBPATH", str(tmp_path / "app.db"))
    import ingest_processor
    return ingest_processor


@pytest.mark.parametrize("outcome", ["applied", "failed"])
def test_the_import_step_never_fails_an_import(monkeypatch, tmp_path, capsys, outcome):
    ip = _processor_module(monkeypatch, tmp_path)
    app_session = MagicMock()
    monkeypatch.setattr(ip, "_CPS_AVAILABLE", True)
    monkeypatch.setattr(ip, "_ub", type("UB", (), {"session": app_session}))
    calibre = MagicMock()
    monkeypatch.setattr(db, "CalibreDB", MagicMock(return_value=calibre, session_factory=object()))
    if outcome == "applied":
        monkeypatch.setattr(iv, "map_new_books", MagicMock(return_value=type("Set", (), {"id": 42})()))
    else:
        monkeypatch.setattr(iv, "map_new_books", MagicMock(side_effect=RuntimeError("database is locked")))
    processor = object.__new__(ip.NewBookProcessor)
    processor.map_incoming_values_if_enabled([7])           # must not raise
    out = capsys.readouterr().out
    if outcome == "applied":
        assert "change set 42" in out
    else:
        assert "not applied to [7]: database is locked" in out
        calibre.session.rollback.assert_called_once()
        app_session.rollback.assert_called_once()
    calibre.session.close.assert_called_once()
