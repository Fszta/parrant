"""Diff-driven impact: turn a set of changes into a consolidated blast radius.

The single-column impact engine answers "what does this one column touch?".
The real unit of work, however, is a *pull request*: a set of changed columns.
This module derives that changeset — either from two dbt artifact sets (base vs.
head, the reliable dbt-native signal) or from a git diff of ``.sql`` files
(fallback when only one manifest is available) — and feeds every changed column
through the existing traversal core, deduplicating downstream nodes.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from parrant.lineage.provider import LineageProvider
from parrant.lineage.semantic_diff import (
    _UNPARSEABLE_PREFIX,
    canonical_key,
    comment_free_token_signature,
    compare_expressions,
)
from parrant.models.schema import (
    LogicDiffStatus,
    OverrideDirective,
    OverrideVerb,
    SemanticChangeKind,
    StructuralDiffStatus,
)
from parrant.parser.sql_parser import parse_override_directives

logger = logging.getLogger(__name__)


class ChangeKind(str, Enum):
    """How a column changed between base and head.

    Ordering matters: when the same column is touched in more than one way (e.g.
    a model whose ``compiled_code`` changed *and* whose column was retyped), the
    highest-priority kind wins. Roughly ordered by blast-radius risk.
    """

    REMOVED = "removed"
    TYPE_CHANGED = "type_changed"
    LOGIC_CHANGED = "logic_changed"
    ADDED = "added"

    @property
    def priority(self) -> int:
        return _KIND_PRIORITY[self]


_KIND_PRIORITY: Dict[ChangeKind, int] = {
    ChangeKind.REMOVED: 5,
    ChangeKind.TYPE_CHANGED: 4,
    ChangeKind.LOGIC_CHANGED: 3,
    ChangeKind.ADDED: 1,
}


@dataclass(frozen=True)
class ColumnChange:
    """A single changed column, with the kind of change and optional detail.

    ``semantic`` carries the AST-diff classification (breaking / non-breaking axis) for
    ``logic_changed`` columns: ``MEANING_CHANGED`` when the expression's meaning changed,
    ``INDETERMINATE`` when a precise per-column diff was impossible (fail-safe). Structural
    kinds (``ADDED`` / ``REMOVED`` / ``TYPE_CHANGED``) leave it ``None`` — their breaking
    nature is already conveyed by the kind.
    """

    model: str
    column: str
    kind: ChangeKind
    detail: Optional[str] = None
    semantic: Optional[SemanticChangeKind] = None
    # Why a ``logic_changed`` column was flagged: the human-readable semantic reason plus the
    # two compared defining expressions. Populated only for logic changes (structural kinds
    # leave them ``None``), and surfaced by ``--explain`` / the JSON ``explain`` block.
    reason: Optional[str] = None
    base_expression: Optional[str] = None
    head_expression: Optional[str] = None
    # the override pragma acknowledging this change, when one resolved to it. Excluded from
    # equality/hashing (``compare=False``) so it never perturbs the sort key or dedup, and so a
    # frozen ``ColumnChange`` stays hashable even though ``OverrideDirective`` (pydantic) is not.
    override: Optional[OverrideDirective] = field(default=None, compare=False)
    # Set to ``"indeterminate"`` when the model-level logic diff was IMPOSSIBLE (compiled SQL
    # unavailable on a side for a model node): "no logic change" was unprovable, so the change
    # is emitted fail-safe rather than silently dropped. ``None`` for every ordinary change.
    # Consumed by the service to degrade confidence and widen the rebuild selection.
    logic_diff_status: Optional[LogicDiffStatus] = None

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "model": self.model,
            "column": self.column,
            "kind": self.kind.value,
            "detail": self.detail,
            "semantic": self.semantic.value if self.semantic else None,
        }
        # Only attach the nested ``explain`` block when we actually computed a reason (i.e. a
        # logic change). Structural add/remove/type entries stay lean and JSON-backward-compatible.
        if self.reason is not None:
            payload["explain"] = {
                "reason": self.reason,
                "base": self.base_expression,
                "head": self.head_expression,
            }
        # Only attach ``logic_diff_status`` when set (the indeterminate missing-SQL case), so
        # ordinary changes keep their existing JSON shape byte-for-byte.
        if self.logic_diff_status is not None:
            payload["logic_diff_status"] = self.logic_diff_status
        # Only attach ``override`` when one is present, so JSON stays byte-stable when absent.
        # Flows into service.by_change automatically (by_change spreads change.to_dict()).
        if self.override is not None:
            payload["override"] = {
                "verb": self.override.verb.value,
                "column": self.override.column,
                "reason": self.override.reason,
                "source_line": self.override.source_line,
                "scope": self.override.scope,
            }
        return payload


def _registry_dialect(registry: object) -> Optional[str]:
    """Best-effort SQL dialect from a registry, ``None`` when it exposes no getter.

    Defensive so a real ``ModelRegistry`` yields its dialect while lightweight test stubs
    (no ``get_dialect``) degrade to ``None`` (sqlglot's default dialect) rather than raising.
    """
    getter = getattr(registry, "get_dialect", None)
    if getter is None:
        return None
    dialect = getter()
    return dialect if isinstance(dialect, str) else None


@dataclass
class OverrideResolution:
    """Collected side-outputs of override resolution: stale directives + parse warnings.

    ``stale`` holds directives whose target column is NOT in the changeset (a dead excuse to
    prune); ``warnings`` holds human strings for malformed / reasonless / unknown-verb pragmas.
    Used as an out-param by :func:`build_git_changeset` (a free function) so its return type
    stays ``List[ColumnChange]`` and existing callers are unaffected.
    """

    stale: List[Dict[str, object]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _stale_record(directive: OverrideDirective) -> Dict[str, object]:
    """The report skeleton for a stale (no matching change) override."""
    record = directive.to_record()
    return record


def _attach_override(change: ColumnChange, directive: OverrideDirective) -> ColumnChange:
    """Attach ``directive`` to ``change`` (frozen => ``dataclasses.replace``), hard-wins.

    Precedence when a column already carries an override: an existing hard ``allow-break`` is
    NEVER downgraded by a later soft ``allow-change`` (the strongest verb wins so the audit
    trail keeps the loudest acknowledgement). Any other combination is last-wins.
    """
    existing = change.override
    if (
        existing is not None
        and existing.verb is OverrideVerb.ALLOW_BREAK
        and directive.verb is OverrideVerb.ALLOW_CHANGE
    ):
        return change
    return dataclasses.replace(change, override=directive)


def resolve_overrides(
    model_to_sql: Dict[str, Optional[str]],
    changes: List[ColumnChange],
) -> Tuple[List[ColumnChange], List[Dict[str, object]], List[str]]:
    """Attach override pragmas parsed from each model's head SQL to the matching changes.

    Shared by :class:`ChangesetBuilder` and :func:`build_git_changeset` so both entry points
    resolve overrides identically. Returns ``(changes, stale, warnings)``:
      * model-scope directive => attached to EVERY changed column of that model; stale when the
        model has zero changed columns;
      * column-scope directive => attached to the (case-insensitive) matching changed column;
        stale when the named/adjacency-resolved column is not in the changeset (or unresolved).
    Names are lowercased to match the ``ColumnChange`` keys.
    """
    result = list(changes)
    changes_by_model: Dict[str, List[int]] = {}
    for idx, change in enumerate(result):
        changes_by_model.setdefault(change.model.lower(), []).append(idx)

    stale: List[Dict[str, object]] = []
    warnings: List[str] = []

    for model_name, sql in model_to_sql.items():
        if not sql:
            # No compiled/raw SQL for this model => no pragmas seen (honest no-op, not a stale).
            continue
        directives, parse_warnings = parse_override_directives(sql)
        for warning in parse_warnings:
            warnings.append(f"{model_name}: {warning}")
        if not directives:
            continue
        idxs = changes_by_model.get(model_name.lower(), [])
        for directive in directives:
            located = directive.model_copy(update={"model": model_name})
            if located.scope == "model" and located.column is None:
                if not idxs:
                    stale.append(_stale_record(located))
                    continue
                for i in idxs:
                    result[i] = _attach_override(result[i], located)
                continue
            # column scope (explicit column= or line-adjacency; column may be None => stale).
            target = None
            if located.column is not None:
                for i in idxs:
                    if result[i].column.lower() == located.column:
                        target = i
                        break
            if target is None:
                stale.append(_stale_record(located))
                continue
            result[target] = _attach_override(result[target], located)

    return result, stale, warnings


@dataclass
class _ColumnDiff:
    """The classification of one signature-differing column, with the data behind it.

    Carries the ``SemanticChangeKind`` verdict *and* the discarded evidence — the human
    reason and the two compared defining expressions — so ``--explain`` can answer "why was
    this column flagged?" instead of surfacing a bare enum.
    """

    kind: SemanticChangeKind
    reason: Optional[str]
    base_expression: Optional[str]
    head_expression: Optional[str]


class ChangesetBuilder:
    """Derive a :class:`ColumnChange` list from two loaded :class:`LineageProvider` sides.

    Compares base (target branch) against head (current) and emits, per column:
    ``added`` / ``removed`` / ``type_changed``, plus ``logic_changed`` for every
    column of a model whose compiled SQL changed. When a column is touched in more
    than one way, the highest-priority kind is kept (see :class:`ChangeKind`).
    """

    def __init__(
        self,
        base: LineageProvider,
        head: LineageProvider,
        dialect: Optional[str] = None,
        honor_overrides: bool = True,
    ):
        self.base = base
        self.head = head
        # The semantic diff canonicalizes expressions per SQL dialect (matters for e.g.
        # Snowflake identifier folding). Resolve it from the head registry when not given,
        # defensively so the 2-arg call and stubs without ``get_dialect`` keep working.
        self._dialect = dialect if dialect is not None else _registry_dialect(head)
        # when True (default), parse override pragmas from head SQL and attach them.
        # ``--no-overrides`` sets this False to compute the raw gate (audit / the backtest).
        self.honor_overrides = honor_overrides
        self.stale_overrides: List[Dict[str, object]] = []
        self.override_warnings: List[str] = []

    def build(self) -> List[ColumnChange]:
        # (model, column) -> ColumnChange, keeping the highest-priority kind.
        chosen: Dict[Tuple[str, str], ColumnChange] = {}

        def record(change: ColumnChange) -> None:
            key = (change.model, change.column)
            existing = chosen.get(key)
            if existing is None or change.kind.priority > existing.kind.priority:
                chosen[key] = change

        base_models = self.base.get_models()
        head_models = self.head.get_models()

        for model_name in sorted(set(base_models) | set(head_models)):
            base_model = base_models.get(model_name)
            head_model = head_models.get(model_name)

            if base_model is not None and head_model is None:
                # Whole model removed: every column it exposed is now gone. Because the
                # universe is manifest-seeded, ``head_model is None`` means the model is
                # absent from the *head manifest* — a genuine deletion — not merely absent
                # from the head catalog (a built-but-uncatalogued model is still present).
                for column in sorted(base_model.columns):
                    record(ColumnChange(model_name, column, ChangeKind.REMOVED))
                continue

            if head_model is not None and base_model is None:
                # Brand-new model: all columns are additive.
                for column in sorted(head_model.columns):
                    record(ColumnChange(model_name, column, ChangeKind.ADDED))
                continue

            assert base_model is not None and head_model is not None

            # Structural column diffs (added/removed/type_changed) are only trustworthy
            # when BOTH sides are backed by a real catalog. For a catalog-missing model
            # the column set is recovered from parsing compiled SQL (best-effort, no data
            # types), so diffing it would emit phantom add/remove/type churn. In that case
            # we rely solely on the compiled-SQL diff below (logic_changed).
            if self._both_catalog_backed(model_name):
                base_cols = base_model.columns
                head_cols = head_model.columns

                for column in sorted(set(base_cols) | set(head_cols)):
                    in_base = column in base_cols
                    in_head = column in head_cols
                    if in_head and not in_base:
                        record(ColumnChange(model_name, column, ChangeKind.ADDED))
                    elif in_base and not in_head:
                        record(ColumnChange(model_name, column, ChangeKind.REMOVED))
                    else:
                        base_type = (base_cols[column].data_type or "").lower()
                        head_type = (head_cols[column].data_type or "").lower()
                        # Only a real type change counts; an unknown type on either side
                        # (empty string) is not evidence of a change.
                        if base_type and head_type and base_type != head_type:
                            record(
                                ColumnChange(
                                    model_name,
                                    column,
                                    ChangeKind.TYPE_CHANGED,
                                    detail=f"{base_cols[column].data_type} -> {head_cols[column].data_type}",
                                )
                            )

            # Logic diff: three-state, never silently safe. ``indeterminate`` (a MODEL whose
            # compiled SQL is unavailable on a side) emits fail-safe changes below — treating
            # it as "no change" would hide the edit AND leave the model out of the rebuild.
            logic_status = self._logic_diff_status(model_name, head_model)
            if logic_status == "indeterminate":
                for change in self._indeterminate_logic_changes(model_name, head_model):
                    record(change)

            # Logic change: the model's compiled SQL differs. Rather than flag EVERY output
            # column — which floods the downstream blast radius with unrelated pass-throughs
            # (editing one column must not implicate every other column of the model) — diff each output
            # column's derivation between base and head and flag ONLY the columns that actually
            # changed. Falls back to flagging all columns when neither side exposes per-column
            # lineage (nothing to diff precisely).
            if logic_status == "changed":
                classified = self._logic_changed_columns(base_model, head_model)
                if not classified:
                    # The compiled SQL provably changed, yet no output column's per-column
                    # signature moved: the edit lives where the signature does not reach —
                    # a JOIN ``ON`` predicate or a WHERE/join-side filter — which can still
                    # shift downstream output *values*. Rather than fall silent and
                    # confidently report "no changes" on a model whose SQL changed (a false
                    # negative at full confidence — the one direction the gate must never
                    # take), fail safe: flag every head output column INDETERMINATE. See #129.
                    classified = self._unattributed_logic_fallback(model_name, head_model)
                for column in sorted(classified):
                    diff = classified[column]
                    record(
                        ColumnChange(
                            model_name,
                            column,
                            ChangeKind.LOGIC_CHANGED,
                            semantic=diff.kind,
                            reason=diff.reason,
                            base_expression=diff.base_expression,
                            head_expression=diff.head_expression,
                        )
                    )

        chosen_changes = list(chosen.values())
        if self.honor_overrides:
            chosen_changes = self._apply_overrides(chosen_changes)
        return sorted(chosen_changes, key=lambda c: (c.model, c.column, c.kind.value))

    def _apply_overrides(self, changes: List[ColumnChange]) -> List[ColumnChange]:
        """Parse override pragmas from each changed model's head SQL and attach them.

        Compiled dbt SQL preserves ``--`` comments, so the head compiled SQL is the pragma
        source. Records stale directives / parse warnings on ``self`` for the report.
        """
        model_to_sql: Dict[str, Optional[str]] = {
            model_name: self._safe_compiled_sql(self.head, model_name)
            for model_name in {change.model for change in changes}
        }
        resolved, stale, warnings = resolve_overrides(model_to_sql, changes)
        self.stale_overrides = stale
        self.override_warnings = warnings
        return resolved

    def _both_catalog_backed(self, model_name: str) -> bool:
        """True when the model has a real catalog entry in BOTH base and head.

        Structural column diffs are only authoritative when column names and data types
        come from the catalog on both sides; otherwise we fall back to the compiled-SQL
        (logic) diff to avoid phantom add/remove/type changes from parser-derived columns.
        """
        return self.base.is_catalog_backed(model_name) and self.head.is_catalog_backed(model_name)

    def structural_diff_available(self) -> bool:
        """Whether add/removed/type_changed detection could run at all.

        Those structural checks require a real catalog on BOTH sides (see
        :meth:`_both_catalog_backed`); without one we fall back to the compiled-SQL
        (logic) diff and cannot see columns added, removed, or retyped. Returns
        ``True`` only when each side contributes at least one catalog-backed model —
        so the report can be honest that structural checks were skipped when a
        ``catalog.json`` is absent on either side.
        """
        return self._side_has_catalog(self.base) and self._side_has_catalog(self.head)

    @staticmethod
    def _side_has_catalog(registry: LineageProvider) -> bool:
        return any(registry.is_catalog_backed(name) for name in registry.get_models())

    def _logic_diff_status(self, model_name: str, head_model: Any) -> LogicDiffStatus:
        """Three-state logic diff for a node present on both sides — never silently safe.

        ``"changed"`` / ``"unchanged"`` when both sides expose compiled SQL to compare.
        When either side's compiled SQL is unavailable the answer depends on the node kind:
        sources / seeds / snapshots legitimately have no logic to diff (``"unchanged"``),
        but a MODEL without compiled SQL cannot be proven unchanged — that is
        ``"indeterminate"``, and the caller emits fail-safe changes for it. The resource
        type is read defensively (``getattr``) so lightweight stubs without one keep the
        historical no-op behaviour; every real registry model carries it.
        """
        base_sql = self._safe_compiled_sql(self.base, model_name)
        head_sql = self._safe_compiled_sql(self.head, model_name)
        if not base_sql or not head_sql:
            if getattr(head_model, "resource_type", None) == "model":
                return "indeterminate"
            return "unchanged"
        if base_sql == head_sql:
            return "unchanged"
        # Gate on the string-literal-safe token signature, NOT a regex comment strip: the
        # tokenizer keeps string/quoted-identifier CONTENTS intact, so a `--` or `/*` inside a
        # quoted string is data — a regex strip would swallow the real tokens around it and
        # silently report "no logic change" (a false SAFE). Equal signatures prove the edit is
        # comment/whitespace-only; anything else — including a side we cannot even tokenize —
        # is treated as changed (fail-safe; the per-column diff classifies it precisely).
        base_sig = comment_free_token_signature(base_sql, self._dialect)
        head_sig = comment_free_token_signature(head_sql, self._dialect)
        if base_sig is not None and head_sig is not None:
            return "changed" if base_sig != head_sig else "unchanged"
        return "changed"

    def _indeterminate_logic_changes(self, model_name: str, head_model: Any) -> List[ColumnChange]:
        """Fail-safe changes for a model whose logic diff was impossible (missing SQL).

        Every head output column is flagged ``LOGIC_CHANGED`` / ``INDETERMINATE`` with the
        ``logic_diff_status`` marker so the service degrades confidence and widens the rebuild
        selection. A column-less model (no catalog entry AND no SQL to parse) still emits one
        ``*`` sentinel change — an unprovable model must never vanish from the report.
        """
        reason = (
            f"compiled SQL for model '{model_name}' is unavailable on at least one side — "
            "the logic diff is impossible, so 'no logic change' cannot be proven (fail-safe)"
        )
        columns = sorted(head_model.columns) or ["*"]
        return [
            ColumnChange(
                model_name,
                column,
                ChangeKind.LOGIC_CHANGED,
                semantic=SemanticChangeKind.INDETERMINATE,
                reason=reason,
                logic_diff_status="indeterminate",
            )
            for column in columns
        ]

    def _logic_changed_columns(self, base_model, head_model) -> Dict[str, "_ColumnDiff"]:
        """Which output columns changed derivation, each with a semantic classification.

        The model's compiled SQL differs, but usually only a few columns are responsible.
        We compare each column's per-column lineage signature (transformation type +
        *canonical* expression + source columns) so downstream tracing follows only the
        columns whose *value* changed — not every pass-through the model happens to expose.
        The expression component is now an AST canonical key, so a purely cosmetic edit
        (whitespace / comments / identifier case / redundant parens) yields identical
        signatures and is suppressed — where the old string compare would have flagged it.

        Returns a ``{column -> _ColumnDiff}`` map (breaking / non-breaking axis, plus the
        reason and compared expressions behind the verdict).
        Conservative fallbacks preserve correctness where a precise diff isn't possible:
        - if NEITHER side has any parsed per-column lineage, flag all head columns as
          ``INDETERMINATE`` (nothing to diff precisely → fail-safe breaking);
        - a column changed on both sides is ``MEANING_CHANGED`` unless an involved
          expression is unparseable, then ``INDETERMINATE``;
        - a column parsed on exactly one side (its derivation appeared/disappeared) is
          ``MEANING_CHANGED`` unless the present side is unparseable, then ``INDETERMINATE``;
        - a column parsed on NEITHER side (no lineage either place) is skipped.
        """
        base_sigs = self._column_signatures(base_model)
        head_sigs = self._column_signatures(head_model)
        if not base_sigs and not head_sigs:
            return {
                column: _ColumnDiff(
                    kind=SemanticChangeKind.INDETERMINATE,
                    reason="no per-column lineage available to diff — treated as breaking (fail-safe)",
                    base_expression=None,
                    head_expression=None,
                )
                for column in head_model.columns
            }

        changed: Dict[str, _ColumnDiff] = {}
        for column in head_model.columns:
            base_sig = base_sigs.get(column)
            head_sig = head_sigs.get(column)
            if base_sig is None and head_sig is None:
                # Neither side parsed this column (e.g. a literal constant with no lineage);
                # there's nothing to diff, so don't treat it as changed.
                continue
            if base_sig == head_sig:
                # Equal signatures ⇒ semantically equal (now covers cosmetic-only edits).
                continue
            changed[column] = self._classify_change(
                self._column_expressions(base_model, column),
                self._column_expressions(head_model, column),
            )
        return changed

    def _unattributed_logic_fallback(self, model_name: str, head_model) -> Dict[str, "_ColumnDiff"]:
        """Fail-safe for a proven compiled-SQL change that no output column can explain.

        ``_logic_changed`` proved the compiled SQL differs, but ``_logic_changed_columns``
        attributed the change to no output column — the edit lives somewhere the per-column
        signature does not fold in (a JOIN ``ON`` predicate, a WHERE/join-side filter), whose
        change can still shift downstream output *values* (issue #129).

        We first tell a genuinely semantic statement-level change apart from a purely cosmetic
        one with a statement-level AST compare: an identifier-case / whitespace-only edit makes
        ``_logic_changed`` (a string compare) fire while the per-column AST diff correctly
        suppresses every column, and that difference is proven ``EQUIVALENT`` here — so we keep
        the confident silence and record nothing. Only a non-``EQUIVALENT`` verdict (a real
        meaning change, or one we cannot parse) trips the backstop, flagging every head output
        column ``INDETERMINATE`` — mirroring the neither-side-parsed fallback so the change is
        never silently dropped and downstream tracing still runs.
        """
        base_sql = self._safe_compiled_sql(self.base, model_name)
        head_sql = self._safe_compiled_sql(self.head, model_name)
        if compare_expressions(base_sql, head_sql, self._dialect).equal:
            # Statement-level difference is provably cosmetic — no output value changed.
            return {}
        reason = (
            "compiled SQL changed but the change could not be attributed to a specific "
            "column (e.g. a JOIN or WHERE predicate) — treated as breaking (fail-safe)"
        )
        return {
            column: _ColumnDiff(
                kind=SemanticChangeKind.INDETERMINATE,
                reason=reason,
                base_expression=None,
                head_expression=None,
            )
            for column in head_model.columns
        }

    def _classify_change(self, base_exprs: List[str], head_exprs: List[str]) -> "_ColumnDiff":
        """Classify a signature-differing column, keeping the reason and compared expressions.

        Fail-safe: if any involved defining expression is unparseable we cannot prove *how*
        it changed, so we stay conservative (``INDETERMINATE`` → breaking). For the common
        single-entry column we defer to ``compare_expressions`` for precision (and reuse its
        ``reason``); a multi-entry (UNION) column with all-parseable branches is a real
        meaning change.
        """
        if self._any_unparseable(base_exprs) or self._any_unparseable(head_exprs):
            return _ColumnDiff(
                kind=SemanticChangeKind.INDETERMINATE,
                reason="could not prove the change — an involved expression did not parse",
                base_expression=" | ".join(e for e in base_exprs if e) or None,
                head_expression=" | ".join(e for e in head_exprs if e) or None,
            )
        if len(base_exprs) == 1 and len(head_exprs) == 1:
            diff = compare_expressions(base_exprs[0], head_exprs[0], self._dialect)
            kind = (
                SemanticChangeKind.INDETERMINATE
                if diff.kind is SemanticChangeKind.INDETERMINATE
                else SemanticChangeKind.MEANING_CHANGED
            )
            return _ColumnDiff(
                kind=kind,
                reason=diff.reason,
                base_expression=base_exprs[0] or None,
                head_expression=head_exprs[0] or None,
            )
        return _ColumnDiff(
            kind=SemanticChangeKind.MEANING_CHANGED,
            reason="multi-branch (e.g. UNION) derivation changed",
            base_expression=" | ".join(base_exprs) or None,
            head_expression=" | ".join(head_exprs) or None,
        )

    def _any_unparseable(self, expressions: List[str]) -> bool:
        return any(
            canonical_key(expression, self._dialect).startswith(_UNPARSEABLE_PREFIX)
            for expression in expressions
        )

    @staticmethod
    def _column_expressions(model, column_name: str) -> List[str]:
        """The raw defining expression string(s) of a column's lineage entries (or ``[]``)."""
        column = model.columns.get(column_name)
        if column is None:
            return []
        lineage = getattr(column, "lineage", None) or []
        return [getattr(entry, "sql_expression", None) or "" for entry in lineage]

    def _column_signatures(self, model) -> Dict[str, Tuple]:
        """Per-column derivation signature: {column -> sorted lineage fingerprint}.

        Columns with no parsed lineage are omitted (no signature), so the caller can tell
        "parsed, unchanged" apart from "not parsed". The expression component is a
        dialect-aware AST canonical key (``canonical_key``), so cosmetic-only differences
        collapse to the same signature.
        """
        signatures: Dict[str, Tuple] = {}
        for column_name, column in model.columns.items():
            lineage = getattr(column, "lineage", None) or []
            if not lineage:
                continue
            parts = []
            for entry in lineage:
                expression = canonical_key(getattr(entry, "sql_expression", None), self._dialect)
                sources = ",".join(sorted(entry.source_columns or []))
                parts.append((entry.transformation_type or "", expression, sources))
            signatures[column_name] = tuple(sorted(parts))
        return signatures

    @staticmethod
    def _safe_compiled_sql(registry: LineageProvider, model_name: str) -> Optional[str]:
        try:
            return registry.get_compiled_sql(model_name)
        except Exception:
            # Sources, seeds and models without compiled SQL simply have no logic
            # to diff; treat as "no logic change".
            return None


def _provider_call(provider: object, method: str) -> Any:
    """Call a zero-arg accessor on a provider defensively (``None`` when absent/failing).

    Mirrors :func:`_registry_dialect`: a real ``ModelRegistry`` answers, a lightweight test
    stub without the accessor simply makes no claim rather than raising.
    """
    getter = getattr(provider, method, None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _parse_artifact_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse a dbt ``generated_at`` ISO timestamp, ``None`` when absent/unparseable."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        # dbt stamps UTC with a trailing ``Z``, which py3.10's fromisoformat rejects.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_structural_degradation(
    base: LineageProvider, head: LineageProvider
) -> Optional[StructuralDiffStatus]:
    """Detect a base/head catalog pair that structurally CANNOT show column changes.

    Two real-deployment failure modes make the structural (added/removed/type_changed) diff
    silently blind while ``structural_diff_available()`` still reports True:

    * **Identical catalogs** — one prod ``catalog.json`` mounted on both sides. Diffing a
      catalog against itself can never surface a removed or retyped column, so
      ``provable_break_count`` is stuck at 0 and ``--fail-on tests`` can never fire.
    * **Stale head catalog** — the head ``catalog.json`` predates the head manifest (and comes
      from a different dbt invocation): its column truth describes an older build.

    Returns a ``degraded`` stamp with a human-readable reason, or ``None`` when neither
    condition is provable (providers without artifact identity — e.g. stubs — make no claim).
    Advisory only: consumed by the report/JSON/markdown surfaces, never by exit codes.
    """
    base_fingerprint = _provider_call(base, "get_catalog_fingerprint")
    head_fingerprint = _provider_call(head, "get_catalog_fingerprint")
    if base_fingerprint is not None and base_fingerprint == head_fingerprint:
        return StructuralDiffStatus(
            status="degraded",
            reason=(
                "base and head catalog.json are identical — one catalog backs both sides, so "
                "removed/type_changed columns can never be detected"
            ),
        )

    stamps = _provider_call(head, "get_artifact_stamps")
    if stamps is not None:
        catalog_stamp = stamps.catalog
        manifest_stamp = stamps.manifest
        same_invocation = (
            catalog_stamp.invocation_id is not None
            and catalog_stamp.invocation_id == manifest_stamp.invocation_id
        )
        if not same_invocation:
            catalog_ts = _parse_artifact_timestamp(catalog_stamp.generated_at)
            manifest_ts = _parse_artifact_timestamp(manifest_stamp.generated_at)
            try:
                stale = (
                    catalog_ts is not None and manifest_ts is not None and catalog_ts < manifest_ts
                )
            except TypeError:  # mixed aware/naive timestamps — no provable claim
                stale = False
            if stale:
                return StructuralDiffStatus(
                    status="degraded",
                    reason=(
                        f"head catalog.json (generated_at {catalog_stamp.generated_at}) predates "
                        f"the head manifest ({manifest_stamp.generated_at}) — its column truth "
                        "is stale, so removed/type_changed detection is unreliable"
                    ),
                )
    return None


def _path_to_model_map(head: LineageProvider) -> Dict[str, str]:
    """Map each model's ``resource_path`` (dbt ``original_file_path``) to its name."""
    mapping: Dict[str, str] = {}
    for model_name, model in head.get_models().items():
        if model.resource_path:
            mapping[_norm_path(model.resource_path)] = model_name
    return mapping


def git_changed_models(
    head: LineageProvider,
    git_base: str,
    repo_dir: Optional[str] = None,
    git_head: str = "HEAD",
) -> Set[str]:
    """Return the set of models whose ``.sql`` file changed between ``git_base`` and ``git_head``.

    Files with no matching model (macros, tests, deleted files) are ignored, so
    this reflects only *model* edits — the unit the scope filter cares about.

    ``git_head`` defaults to ``HEAD`` so existing callers (impact, explorer) are byte-identical;
    the backtest passes a specific commit to replay a single historical point.
    """
    matched, _unmapped = git_changed_models_and_unmapped(head, git_base, git_head, repo_dir)
    return matched


def git_changed_models_and_unmapped(
    head: LineageProvider,
    git_base: str,
    git_head: str = "HEAD",
    repo_dir: Optional[str] = None,
) -> Tuple[Set[str], List[str]]:
    """Split changed ``.sql`` files into (models that mapped, paths that did NOT).

    Reuses the same path->model map and git diff as :func:`git_changed_models` but also returns
    the unmapped ``.sql`` paths so the backtest can report ``unmapped_changes`` honestly: the
    HEAD registry replayed against an older diff cannot map models added/renamed since a commit
    (spec honesty invariant). Non-model SQL (macros/tests/snapshots) shows up here too.
    """
    path_to_model = _path_to_model_map(head)
    matched: Set[str] = set()
    unmapped: List[str] = []
    for changed_file in _git_changed_sql_files(git_base, repo_dir, git_head):
        model = path_to_model.get(_norm_path(changed_file))
        if model:
            matched.add(model)
        else:
            unmapped.append(changed_file)
    return matched, unmapped


def git_rev_list(base: str, head: str, repo_dir: Optional[str] = None) -> List[str]:
    """Enumerate commits in ``base..head`` (oldest -> newest) that touch a ``.sql`` file.

    Each surviving commit is replayed as one changeset (one commit ≈ one squash-merged PR).
    Filtered to commits touching ``*.sql`` so a macro/doc-only commit does not become a
    zero-change replay point. Raises :class:`RuntimeError` on git failure (mirrors
    :func:`_git_changed_sql_files`).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--reverse", f"{base}..{head}", "--", "*.sql"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"Failed to enumerate commits '{base}..{head}': {exc}") from exc
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changes_from_dicts(entries: List[Dict[str, Any]]) -> List[ColumnChange]:
    """Reconstruct :class:`ColumnChange` objects from the ``changeset.changes`` JSON shape.

    Accepts the dicts produced by :meth:`ColumnChange.to_dict` (model/column/kind + optional
    detail/semantic/explain). The ``override`` field is intentionally NOT restored — the backtest
    runs ``honor_overrides=False`` (it measures the RAW gate), and base/head
    expressions/reason are not needed for policy evaluation. Unknown/malformed entries raise a
    ``ValueError`` (via the enum constructors) so a corrupt corpus fails loudly.
    """
    changes: List[ColumnChange] = []
    for entry in entries:
        semantic_raw = entry.get("semantic")
        semantic = SemanticChangeKind(semantic_raw) if semantic_raw else None
        explain = entry.get("explain") or {}
        changes.append(
            ColumnChange(
                model=str(entry["model"]),
                column=str(entry["column"]),
                kind=ChangeKind(entry["kind"]),
                detail=entry.get("detail"),
                semantic=semantic,
                reason=explain.get("reason"),
                base_expression=explain.get("base"),
                head_expression=explain.get("head"),
            )
        )
    return changes


def build_git_changeset(
    head: LineageProvider,
    git_base: str,
    repo_dir: Optional[str] = None,
    honor_overrides: bool = True,
    collect: Optional[OverrideResolution] = None,
    git_head: str = "HEAD",
) -> List[ColumnChange]:
    """Fallback changeset: diff ``.sql`` model files between ``git_base`` and ``git_head``.

    When only one manifest is available we cannot diff columns, so every column
    of each touched model is reported as ``logic_changed`` — a coarse but honest
    signal. Files are mapped to models via each model's ``resource_path``.

    ``git_head`` defaults to ``HEAD`` so existing callers are byte-identical; the backtest
    passes a specific commit (with ``git_base`` = its parent) to replay one historical point.

    when ``honor_overrides`` (default), override pragmas from head SQL are attached to
    the logic changes; there are no provable breaks here so ``allow-break`` has nothing to
    demote, but ``allow-change`` still suppresses the meaning-shift/reach review. Stale
    directives and parse warnings are written to ``collect`` when supplied (the return type
    stays ``List[ColumnChange]`` so existing callers are unaffected).
    """
    changed_models = git_changed_models(head, git_base, repo_dir, git_head)
    if not changed_models:
        return []

    head_models = head.get_models()
    chosen: Dict[Tuple[str, str], ColumnChange] = {}
    for model_name in changed_models:
        model = head_models[model_name]
        for column in sorted(model.columns):
            chosen[(model_name, column)] = ColumnChange(
                model_name,
                column,
                ChangeKind.LOGIC_CHANGED,
                detail=model.resource_path,
                # Single-manifest fallback: we diffed whole .sql files, not columns, so we
                # cannot prove which derivations changed — honestly conservative-breaking.
                semantic=SemanticChangeKind.INDETERMINATE,
            )

    changes = sorted(chosen.values(), key=lambda c: (c.model, c.column))
    if honor_overrides:
        model_to_sql: Dict[str, Optional[str]] = {
            model_name: ChangesetBuilder._safe_compiled_sql(head, model_name)
            for model_name in changed_models
        }
        changes, stale, warnings = resolve_overrides(model_to_sql, changes)
        if collect is not None:
            collect.stale = stale
            collect.warnings = warnings
    return changes


def scope_changes_to_models(changes: List[ColumnChange], models: Set[str]) -> List[ColumnChange]:
    """Keep only changes whose model is in ``models``.

    Used to intersect a precise two-manifest changeset with the set of models
    the current branch actually touched (``git diff base...HEAD``), so a stale
    base artifact can't leak already-merged changes into the report.
    """
    return [change for change in changes if change.model in models]


# Changed files that can influence models' compiled SQL. A change to one of these that
# cannot be pinned to specific models makes git scoping unsound (see resolve_git_scope):
#   *.sql  — a model (mappable) OR a macro/generic test that can rewrite ANY model's SQL
#   *.py   — a Python model (mappable when in the manifest; otherwise unknown logic)
#   *.csv  — a seed: column changes ripple into downstream models without any .sql edit
_LOGIC_BEARING_SUFFIXES = (".sql", ".py", ".csv")
# Project-level files whose edits (vars, packages, profiles/targets) can change the
# compiled SQL of arbitrarily many models and are never mappable to one model.
_LOGIC_BEARING_BASENAMES = frozenset(
    {
        "dbt_project.yml",
        "dbt_project.yaml",
        "packages.yml",
        "packages.yaml",
        "package-lock.yml",
        "dependencies.yml",
        "profiles.yml",
        "profiles.yaml",
    }
)


@dataclass
class GitScopeResolution:
    """How the git diff resolved for ``--scope-git``.

    ``models`` is the union of models the diff could be pinned to (model files by
    ``resource_path``, macros by their dependent models when the backend can map them).
    ``unmappable`` lists logic-bearing changed files that could NOT be pinned to models;
    when non-empty, narrowing the changeset to ``models`` is unsound and the caller must
    disable scoping for the run (fail-safe) rather than silently drop changes.
    Docs/property-only files (schema.yml, *.md, ...) influence neither compiled SQL nor
    the catalog, so they are ignored and never disable scoping.
    """

    models: Set[str] = field(default_factory=set)
    unmappable: List[str] = field(default_factory=list)


def _is_logic_bearing(path: str) -> bool:
    """Whether a changed file can influence some model's compiled SQL / columns."""
    basename = path.rsplit("/", 1)[-1].lower()
    return basename in _LOGIC_BEARING_BASENAMES or basename.endswith(_LOGIC_BEARING_SUFFIXES)


def _macro_dependents_or_empty(head: LineageProvider) -> Dict[str, Set[str]]:
    """Macro-file -> dependent-model map, when the backend can provide it.

    Capability lookup (not part of the :class:`LineageProvider` protocol): backends that
    cannot map macros return ``{}``, which downgrades macro changes to *unmappable* — the
    fail-safe floor — instead of crashing or, worse, silently narrowing.
    """
    getter = getattr(head, "get_macro_dependents", None)
    if not callable(getter):
        return {}
    try:
        dependents = getter()
    except Exception:  # pragma: no cover - honest degrade to the fail-safe floor
        logger.warning("get_macro_dependents failed; macro changes treated as unmappable")
        return {}
    return dependents if isinstance(dependents, dict) else {}


def resolve_git_scope(
    head: LineageProvider,
    git_base: str,
    repo_dir: Optional[str] = None,
    git_head: str = "HEAD",
) -> GitScopeResolution:
    """Classify the FULL git diff (not just ``*.sql``) for ``--scope-git``.

    Unlike :func:`git_changed_models` (which silently ignores files it cannot map — fine
    for the coarse ``--git-base`` fallback where unmapped files simply add no changes),
    scoping *subtracts* from a correct two-manifest changeset, so it must prove that every
    logic-bearing changed file was accounted for. Each changed file is either:

    * mapped to a model via its ``resource_path`` (``.sql`` and ``.py`` models, seeds), or
    * mapped to its dependent models via the manifest macro graph (when available), or
    * logic-bearing but unmappable -> recorded in ``unmappable`` (disables scoping), or
    * non-logic (schema.yml/docs/README/...) -> ignored.
    """
    path_to_model = _path_to_model_map(head)
    macro_dependents = _macro_dependents_or_empty(head)
    models: Set[str] = set()
    unmappable: List[str] = []
    for changed_file in _git_changed_files(git_base, repo_dir, git_head):
        path = _norm_path(changed_file)
        model = path_to_model.get(path)
        if model:
            models.add(model)
            continue
        if path in macro_dependents:
            # Ceiling: the macro is known, so scope to every model it (transitively)
            # feeds instead of disabling scoping outright.
            models.update(macro_dependents[path])
            continue
        if _is_logic_bearing(path):
            unmappable.append(path)
    return GitScopeResolution(models=models, unmappable=unmappable)


def _norm_path(path: str) -> str:
    return re.sub(r"^\./", "", path.strip()).lstrip("/")


def _git_changed_files(git_base: str, repo_dir: Optional[str], git_head: str = "HEAD") -> List[str]:
    """Every changed file between ``git_base`` and ``git_head`` (no pathspec filter).

    :func:`resolve_git_scope` needs the complete diff — macros, Python models,
    ``dbt_project.yml`` — not only the ``*.sql`` view that :func:`_git_changed_sql_files`
    serves to the coarse git fallback and the backtest.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{git_base}...{git_head}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"Failed to compute git diff against '{git_base}': {exc}") from exc

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_changed_sql_files(
    git_base: str, repo_dir: Optional[str], git_head: str = "HEAD"
) -> List[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{git_base}...{git_head}", "--", "*.sql"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"Failed to compute git diff against '{git_base}': {exc}") from exc

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _parrant_version() -> Optional[str]:
    """The installed parrant version, or ``None`` when metadata is unavailable."""
    try:
        from importlib.metadata import version

        return version("parrant")
    except Exception:  # noqa: BLE001 - advisory field; never fail the report over metadata
        return None


def build_changeset_report(
    source: str,
    changes: List[ColumnChange],
    aggregated: Dict[str, object],
) -> Dict[str, object]:
    """Assemble the final report: a ``changeset`` block plus the aggregated impact.

    The impact keys (``summary``, ``affected_models``, ``affected_columns``,
    ``affected_exposures``) are a superset of the single-column ``impact`` block,
    so existing consumers keep working; ``changeset`` and ``by_change`` are added.
    ``report_version`` / ``parrant_version`` identify the report shape and producer so a
    machine consumer can pin what it parses.
    """
    by_kind: Dict[str, int] = {}
    for change in changes:
        by_kind[change.kind.value] = by_kind.get(change.kind.value, 0) + 1

    report: Dict[str, object] = {
        "report_version": 1,
        "parrant_version": _parrant_version(),
        "changeset": {
            "source": source,
            "total_changes": len(changes),
            "by_kind": by_kind,
            "changes": [change.to_dict() for change in changes],
        },
    }
    report.update(aggregated)
    return report
