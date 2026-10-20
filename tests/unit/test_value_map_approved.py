# -*- coding: utf-8 -*-
"""Approved values (#2438's "only allow approved values", opt-in).

Approving a value stores it as an ``X -> X`` rule, so the approved list
survives a value no book carries. The per-field "only approved values" switch
drops, wherever the map is applied, any value no rule covers.
"""
import pytest

import cps.api.value_maps as routes
from cps.services import value_map as vm
from tests.unit.value_map_world import READER, app, app_db, call, library  # noqa: F401

pytestmark = pytest.mark.unit


def _rules(app_db, field="tags"):  # noqa: F811
    return {r.incoming: list(r.targets) for r in vm._rules_of(app_db, field)}


def _save(app_db, raw, field="tags"):  # noqa: F811
    rules, errors = vm.clean_rules(field, raw)
    assert errors == []
    vm.replace_rules(app_db, field, rules, 1)


def test_approved_only_drops_values_no_rule_covers(app_db):  # noqa: F811
    _save(app_db, {"Suspense": ["Thriller"], "General": []})
    vm.approve(app_db, "tags", ["Fantasy"], 1)
    # Off: unmatched values pass through untouched.
    assert vm.apply_value_map("tags", ["Suspense", "General", "Fantasy", "FIC030000"], app_db) == (
        ["Thriller", "Fantasy", "FIC030000"], [])
    vm.set_approved_only(app_db, "tags", True, 1)
    # A rule's drop is not a rejection; only what nothing covers is.
    assert vm.apply_value_map("tags", ["Suspense", "General", "Fantasy", "FIC030000"], app_db) == (
        ["Thriller", "Fantasy"], ["FIC030000"])


def test_saving_rules_keeps_approvals(app_db):  # noqa: F811
    vm.approve(app_db, "tags", ["Fantasy", "Horror"], 1)
    _save(app_db, {"Suspense": ["Thriller"]})
    _save(app_db, {"Crime": ["Mystery"]})
    assert _rules(app_db) == {"Fantasy": ["Fantasy"], "Horror": ["Horror"], "Crime": ["Mystery"]}


def test_approve_skips_values_a_rule_already_changes_and_unapprove(app_db):  # noqa: F811
    _save(app_db, {"Suspense": ["Thriller"]})
    assert vm.approve(app_db, "tags", ["Suspense", "Thriller", "Horror"], 1) == ["Suspense"]
    # Thriller is already approved as a target, so no X -> X row is added for it.
    assert _rules(app_db) == {"Suspense": ["Thriller"], "Horror": ["Horror"]}
    vm.unapprove(app_db, "tags", ["Horror", "Thriller"])
    assert _rules(app_db) == {"Suspense": ["Thriller"]}


def test_a_tags_page_rename_keeps_an_approval_of_the_new_name(app_db):  # noqa: F811
    _save(app_db, {"Suspense": ["Thrillers"], "Thriller": []})
    vm.approve(app_db, "tags", ["Thrillers"], 1)
    vm.record_rename(app_db, "tags", "Thrillers", "Thriller", 1)
    # Thriller was dropped; the rename says it is wanted, so that drop goes.
    assert _rules(app_db) == {"Suspense": ["Thriller"], "Thrillers": ["Thriller"]}


def test_the_switch_drops_unapproved_values_in_the_clean_up_preview(call):
    call(routes.value_map_approve, "tags", method="POST", json={"names": ["Dragons"]})
    status, state = call(routes.value_map_settings, "tags", method="PUT", json={"approved_only": True})
    assert status == 200 and state["approved_only"] is True and state["approved"] == ["Dragons"]
    statuses = {v["name"]: v["status"] for v in state["values"]}
    assert statuses == {"Fiction / Thrillers": "unapproved", "thriller": "unapproved",
                        "General": "unapproved", "Dragons": "approved"}
    # Every book loses the values nobody approved; Dragons stays.
    assert {c["book_id"]: c["after"] for c in state["changes"]} == {1: [], 2: []}


def test_the_approval_routes_are_admin_only_and_validated(call):
    assert call(routes.value_map_approve, "tags", user=READER, method="POST", json={"names": ["X"]})[0] == 403
    assert call(routes.value_map_settings, "tags", user=READER, method="PUT", json={"approved_only": True})[0] == 403
    assert call(routes.value_map_approve, "tags", method="POST", json={"names": [""]})[0] == 400
    assert call(routes.value_map_settings, "tags", method="PUT", json={"approved_only": "yes"})[0] == 400
