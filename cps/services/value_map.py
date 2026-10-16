# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Admin-managed value map for list metadata (#2438, Discussion #2481).

One stored map, read the same way wherever values come in. A rule matches an
incoming value case- and accent-insensitively and turns it into zero or more
target values: one target renames, several split, none drops. A target is also
the canonical spelling of itself, so "thriller" becomes "Thriller" once a rule
names "Thriller" as a target. Values no rule covers are kept as they are.

This module holds the matching and proposes library changes; writing them is
the change set's job (``services/change_sets.py``). It does not decide where
the map is applied; callers do.
"""
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass, field as dc_field

from sqlalchemy import func

from .. import db
from .fields import FIELD_SPECS


def fold(value):
    """Matching key: accents stripped, case folded, inner whitespace collapsed."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def clean_rules(field, raw):
    """Validate a rule list from the API or an import.

    ``raw`` is a list of ``{"incoming": str, "targets": [str, ...]}`` or a
    mapping ``{incoming: [targets]}`` (the shape of a JSON export). Returns
    ``(rules, errors)``; ``rules`` is a list of dicts with ``incoming``,
    ``incoming_key`` and ``targets``.
    """
    spec = FIELD_SPECS.get(field)
    if spec is None:
        return [], ["unknown field"]
    if isinstance(raw, dict):
        raw = [{"incoming": k, "targets": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return [], ["rules must be a list or an object"]
    rules, errors, seen = [], [], {}
    for index, item in enumerate(raw):
        incoming = item.get("incoming") if isinstance(item, dict) else None
        targets = item.get("targets") if isinstance(item, dict) else None
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(incoming, str) or not incoming.strip():
            errors.append(f"rule {index + 1}: the incoming value is empty")
            continue
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            errors.append(f"rule {index + 1}: targets must be a list of text values")
            continue
        incoming = " ".join(incoming.split())
        targets = [" ".join(t.split()) for t in targets if t.strip()]
        if spec.forbid_comma and any("," in v for v in [incoming, *targets]):
            errors.append(f"rule {index + 1}: a value cannot contain a comma")
            continue
        key = fold(incoming)
        if key in seen:
            errors.append(f"rule {index + 1}: '{incoming}' repeats rule {seen[key] + 1}")
            continue
        seen[key] = index
        deduped = []
        for target in targets:
            if fold(target) not in {fold(t) for t in deduped}:
                deduped.append(target)
        if not spec.multi and len(deduped) > 1:
            errors.append(f"rule {index + 1}: a single-valued field maps to one value or none")
            del seen[key]
            continue
        rules.append({"incoming": incoming, "incoming_key": key, "targets": deduped})
    return rules, errors


@dataclass
class ValueMap:
    rules: dict = dc_field(default_factory=dict)       # incoming_key -> [targets]
    canonical: dict = dc_field(default_factory=dict)   # fold(target) -> target: the approved values
    approved_only: bool = False                        # drop values no rule covers

    @classmethod
    def from_rules(cls, rules, approved_only=False):
        vm = cls(approved_only=approved_only)
        for rule in rules:
            vm.rules[rule["incoming_key"]] = list(rule["targets"])
            for target in rule["targets"]:
                vm.canonical.setdefault(fold(target), target)
        return vm

    def covers(self, value):
        key = fold(value)
        return key in self.rules or key in self.canonical

    def map_values(self, values):
        """Map a book's values, order kept.

        A value a rule produced (or re-cased to its canonical target) is
        de-duplicated case- and accent-insensitively. A value no rule covers
        is left as it is, so only an exact repeat of it is dropped: merging
        "Montreal" with "Montréal" is a rule's job, not a side effect. With
        ``approved_only`` a value no rule covers is dropped instead.
        """
        out, folded, exact = [], set(), set()
        for value in values:
            key = fold(value)
            if key in self.rules:
                produced = self.rules[key]
            elif key in self.canonical:
                produced = [self.canonical[key]]
            else:
                if not self.approved_only and value not in exact:
                    exact.add(value)
                    out.append(value)
                continue
            for item in produced:
                item_key = fold(item)
                if item_key not in folded:
                    folded.add(item_key)
                    exact.add(item)
                    out.append(item)
        return out


def apply_value_map(field, values, app_session=None):
    """Map ``values`` for ``field`` with the stored rules: ``(kept, rejected)``.

    The single entry point every caller uses (clean-up preview, ingest,
    automatic and manual fetch). With "only approved values" on for the field,
    a value no rule covers is rejected: approved values are the map's targets
    (an approval is a ``X -> X`` rule). A value a rule drops is not rejected,
    it has already been ruled on.
    """
    if app_session is None:
        from .. import ub
        app_session = ub.session
    value_map = load_map(app_session, field)
    rejected = [v for v in values if value_map.approved_only and not value_map.covers(v)]
    return value_map.map_values(values), rejected


def approved_only(app_session, field):
    from .. import ub
    row = app_session.get(ub.MetadataValueField, field)
    return bool(row and row.approved_only)


def load_map(app_session, field):
    from .. import ub
    rows = (app_session.query(ub.MetadataValueRule)
            .filter(ub.MetadataValueRule.field == field)
            .order_by(ub.MetadataValueRule.incoming_key).all())
    return ValueMap.from_rules(
        ({"incoming_key": r.incoming_key, "targets": r.targets or []} for r in rows),
        approved_only=approved_only(app_session, field),
    )


def is_approval(incoming, targets):
    """A ``X -> X`` rule: it approves X rather than changing anything."""
    return len(targets) == 1 and fold(targets[0]) == fold(incoming)


def _book_values(book, field):
    return [row.name for row in getattr(book, FIELD_SPECS[field].link_attr)]


def library_values(calibre_session, field):
    """Every value in the library with its book count, for the admin list."""
    spec = FIELD_SPECS[field]
    column = spec.link_table.c[spec.link_column]
    rows = (calibre_session.query(spec.model.name, func.count(column))
            .outerjoin(spec.link_table, column == spec.model.id)
            .group_by(spec.model.id).all())
    return sorted(({"name": name, "count": count} for name, count in rows),
                  key=lambda item: (-item["count"], fold(item["name"])))


def plan(calibre_session, value_map, field, book_ids=None):
    """Rows for a change set: books whose values the map would change. Nothing is written."""
    query = calibre_session.query(db.Books)
    if book_ids is not None:
        query = query.filter(db.Books.id.in_(list(book_ids)))
    changes = []
    for book in query.order_by(db.Books.sort).all():
        before = _book_values(book, field)
        after = value_map.map_values(before)
        if after != before:
            changes.append({"book_id": book.id, "field": field, "old": before, "new": after})
    return changes


# --- The stored map, changed one value at a time ----------------------------
# Used by the clean-up page (approve, unapprove) and by the Tags page while
# "only approved values" is on (a rename records a rule, a delete a drop).

def _rules_of(app_session, field):
    from .. import ub
    return app_session.query(ub.MetadataValueRule).filter(ub.MetadataValueRule.field == field).all()


def _put(app_session, field, incoming, targets, user_id):
    from .. import ub
    key = fold(incoming)
    row = (app_session.query(ub.MetadataValueRule)
           .filter(ub.MetadataValueRule.field == field, ub.MetadataValueRule.incoming_key == key).first())
    if row is None:
        row = ub.MetadataValueRule(field=field, incoming=incoming, incoming_key=key)
        app_session.add(row)
    row.incoming, row.targets = incoming, list(targets)
    row.updated_by, row.updated_at = user_id, datetime.now(timezone.utc)
    return row


def replace_rules(app_session, field, rules, user_id):
    """Replace the rename/split/drop rules from a Save or an import. Approvals
    (``X -> X``) already stored are kept unless the new rules rule on X."""
    from .. import ub
    incoming_keys = {r["incoming_key"] for r in rules}
    for row in _rules_of(app_session, field):
        if not is_approval(row.incoming, row.targets or []) or row.incoming_key in incoming_keys:
            app_session.delete(row)
    app_session.flush()
    for rule in rules:
        _put(app_session, field, rule["incoming"], rule["targets"], user_id)
    app_session.commit()


def approve(app_session, field, names, user_id):
    """Approve values with no rule yet. Returns the names skipped because a
    rule already renames or drops them."""
    value_map = load_map(app_session, field)
    skipped = []
    for name in names:
        key = fold(name)
        if key in value_map.canonical:
            continue
        if key in value_map.rules:
            skipped.append(name)
            continue
        _put(app_session, field, name, [name], user_id)
        value_map.canonical[key] = name
    app_session.commit()
    return skipped


def unapprove(app_session, field, names):
    """Take values off the approved list: their ``X -> X`` rules go. A value
    that is also another rule's target stays approved through that rule."""
    keys = {fold(n) for n in names}
    for row in _rules_of(app_session, field):
        if row.incoming_key in keys and is_approval(row.incoming, row.targets or []):
            app_session.delete(row)
    app_session.commit()


def record_rename(app_session, field, old, new, user_id):
    """A rename or merge of ``old`` into ``new`` on the Tags page: ``old -> new``,
    and every rule that produced ``old`` now produces ``new``. A rule that
    renamed or dropped ``new`` itself goes: the rename says ``new`` is wanted."""
    old_key, new_key = fold(old), fold(new)
    for row in _rules_of(app_session, field):
        targets = row.targets or []
        if row.incoming_key == new_key and new_key != old_key and not is_approval(row.incoming, targets):
            app_session.delete(row)
            continue
        if any(fold(t) == old_key for t in targets) and row.incoming_key != old_key:
            merged = []
            for t in targets:
                value = new if fold(t) == old_key else t
                if fold(value) not in {fold(m) for m in merged}:
                    merged.append(value)
            row.targets = merged
    _put(app_session, field, old, [new], user_id)
    app_session.commit()


def record_delete(app_session, field, name, user_id):
    """A delete on the Tags page: ``name`` is dropped from now on, and no rule
    produces it any more (a rule left with no target becomes a drop)."""
    key = fold(name)
    for row in _rules_of(app_session, field):
        targets = row.targets or []
        if row.incoming_key != key and any(fold(t) == key for t in targets):
            row.targets = [t for t in targets if fold(t) != key]
    _put(app_session, field, name, [], user_id)
    app_session.commit()


def rules_snapshot(app_session, field):
    """``{incoming_key: (incoming, targets)}`` for one field, to diff around an edit."""
    return {r.incoming_key: (r.incoming, list(r.targets or [])) for r in _rules_of(app_session, field)}


def restore_rule(app_session, field, incoming, targets, user_id):
    """Put one rule back as it was: ``targets`` None means it did not exist."""
    from .. import ub
    key = fold(incoming)
    row = (app_session.query(ub.MetadataValueRule)
           .filter(ub.MetadataValueRule.field == field, ub.MetadataValueRule.incoming_key == key).first())
    if targets is None:
        if row is not None:
            app_session.delete(row)
    else:
        _put(app_session, field, incoming, targets, user_id)


def set_approved_only(app_session, field, on, user_id):
    from .. import ub
    row = app_session.get(ub.MetadataValueField, field)
    if row is None:
        row = ub.MetadataValueField(field=field)
        app_session.add(row)
    row.approved_only, row.updated_by, row.updated_at = bool(on), user_id, datetime.now(timezone.utc)
    app_session.commit()
