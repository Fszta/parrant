"""Metadata-agnostic policy engine: evaluate consumer rules over a changeset + its lineage
reach + arbitrary dbt meta, producing a PolicyVerdict (gate decision + build/test sets +
notifications).

The tool ships the ENGINE and the rule schema; the consumer ships the rules (policy.yml).
No metadata key is privileged. Fail-closed: anything not proven safe is treated as breaking
(see MissingMetaPolicy and the semantic-absent handling in §7 of the spec).

This module consumes three inputs, each owned elsewhere:
  * ColumnChange.semantic  -- the breaking/non-breaking signal (Step 1, semantic_diff.py)
  * ImpactView             -- reached downstream nodes + mechanism (built from service.py's
                              get_changeset_impact; see build_impact_view)
  * MetaIndex -- ANY dbt meta on model/column/exposure (registry.py accessors)
It NEVER re-walks lineage itself: reach is read from the impact view.

Three-valued evaluation
-----------------------
Leaves resolve to TRUE / FALSE / UNKNOWN. UNKNOWN models "not proven either way", and it
carries its *cause* so the two fail-safe knobs stay independent:
  * ``UNKNOWN_MISSING`` — a missing meta key or an unresolved reach -> governed by
    ``on_missing_meta``;
  * ``UNKNOWN_ERROR`` — an operator/type mismatch or evaluation error (e.g. ``subset_of`` on a
    scalar) -> governed by the SEPARATE ``on_error`` knob.
Combinators use Kleene logic; when a still-UNKNOWN result mixes both causes, ERROR wins so a
genuine type error is never masked by a fail-open missing-meta default. A predicate that stays
UNKNOWN at the rule level is resolved by the matching fail-safe policy: a *blocking* rule under
``fail_closed`` fires (bias toward safety); a non-blocking rule does not (never manufacture a
spurious warning) — but the suppression is SURFACED as ``PolicyVerdict.unproven`` telemetry so
an all-warn pilot policy never understates what block mode will do (issue #124); the opt-in
``defaults.warn_rules_fire_on_unknown`` knob makes the non-blocking rule fire too, at its own
severity. ``fail_open`` never fires on UNKNOWN; ``skip`` drops the rule for that subject
(recorded in ``skipped_missing_meta``).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Protocol, Set, Tuple

import yaml

from parrant.lineage.changeset import ColumnChange
from parrant.models.schema import (
    Action,
    ActionKind,
    BreakFinding,
    ChangeCondition,
    GateDecision,
    Mechanism,
    MetaCondition,
    MissingMetaPolicy,
    Notification,
    Operator,
    OverrideVerb,
    Policy,
    PolicyVerdict,
    Predicate,
    ReachCondition,
    ReachKind,
    Rule,
    RuleHit,
    SemanticChangeKind,
    StructuralCondition,
    UnprovenPolicyHit,
)
from parrant.lineage.verdict import ineffective_override_record


class PolicyConfigError(Exception):
    """A present-but-invalid policy file. A broken policy fails loudly, never silently allows."""


# The one schema major the engine understands. An unknown major is rejected at load.
_SUPPORTED_VERSION = 1

# Cap on names carried in a RuleHit.matched_reach, mirroring the impact confidence name cap so
# a huge blast radius doesn't bloat the verdict payload.
_MATCHED_REACH_NAME_CAP = 100


# --- three-valued logic -----------------------------------------------------


class Tri(Enum):
    """Three-valued logic, but UNKNOWN is split by *cause* so the two fail-safe knobs stay
    independent: ``UNKNOWN_MISSING`` (missing meta / unresolved reach -> resolved by
    ``on_missing_meta``) vs ``UNKNOWN_ERROR`` (operator/type mismatch -> resolved by ``on_error``).
    Both behave identically for Kleene combination; they differ only at :meth:`PolicyEngine._resolve`.
    """

    TRUE = "true"
    FALSE = "false"
    UNKNOWN_MISSING = "unknown_missing"
    UNKNOWN_ERROR = "unknown_error"


def _tri(value: bool) -> Tri:
    return Tri.TRUE if value else Tri.FALSE


def _is_unknown(value: Tri) -> bool:
    return value is Tri.UNKNOWN_MISSING or value is Tri.UNKNOWN_ERROR


def _merge_unknown(values: List[Tri]) -> Tri:
    """Collapse the surviving UNKNOWNs to a single cause. ERROR dominates MISSING so a genuine
    type error is never masked by a fail-open missing-meta default (fail-safe bias)."""
    if any(v is Tri.UNKNOWN_ERROR for v in values):
        return Tri.UNKNOWN_ERROR
    return Tri.UNKNOWN_MISSING


def _and(values: List[Tri]) -> Tri:
    """Kleene AND: any FALSE -> FALSE; else any UNKNOWN -> UNKNOWN; else TRUE. Empty -> TRUE."""
    if any(v is Tri.FALSE for v in values):
        return Tri.FALSE
    unknowns = [v for v in values if _is_unknown(v)]
    if unknowns:
        return _merge_unknown(unknowns)
    return Tri.TRUE


def _or(values: List[Tri]) -> Tri:
    """Kleene OR: any TRUE -> TRUE; else any UNKNOWN -> UNKNOWN; else FALSE. Empty -> FALSE."""
    if any(v is Tri.TRUE for v in values):
        return Tri.TRUE
    unknowns = [v for v in values if _is_unknown(v)]
    if unknowns:
        return _merge_unknown(unknowns)
    return Tri.FALSE


def _not(value: Tri) -> Tri:
    if value is Tri.TRUE:
        return Tri.FALSE
    if value is Tri.FALSE:
        return Tri.TRUE
    return value  # UNKNOWN negates to itself, preserving its cause


# --- config loading ---------------------------------------------------------


def load_policy(path: Optional[str]) -> Optional[Policy]:
    """Resolve and parse the policy file.

    Resolution order (first found): explicit ``path`` -> ``./parrant.policy.yml`` ->
    ``./dbt-col-lineage.policy.yml`` (legacy fallback). Returns ``None`` when no policy is
    configured, so the caller falls back to the legacy ``decide_verdict`` gate (backward
    compatible). Raises :class:`PolicyConfigError` on a present-but-invalid file.
    """
    resolved = _resolve_policy_path(path)
    if resolved is None:
        return None
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except OSError as exc:
        raise PolicyConfigError(f"could not read policy file '{resolved}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"policy file '{resolved}' is not valid YAML: {exc}") from exc
    return parse_policy(raw, source=str(resolved))


def parse_policy(raw: Any, source: str = "<policy>") -> Policy:
    """Validate a parsed policy mapping into a :class:`Policy`.

    Separated from file I/O so tests (and pyproject inlining) can validate a dict directly.
    Rejects a null document, an unknown major version, and any malformed predicate/rule.
    """
    if raw is None:
        raise PolicyConfigError(f"policy '{source}' is empty")
    if not isinstance(raw, dict):
        raise PolicyConfigError(f"policy '{source}' must be a mapping, got {type(raw).__name__}")
    version = raw.get("version")
    if version != _SUPPORTED_VERSION:
        raise PolicyConfigError(
            f"policy '{source}' declares unsupported version {version!r}; "
            f"this engine understands version {_SUPPORTED_VERSION}"
        )
    try:
        return Policy.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> a loud, actionable config error
        raise PolicyConfigError(f"policy '{source}' is invalid: {exc}") from exc


def _resolve_policy_path(path: Optional[str]) -> Optional[str]:
    """First existing path among explicit -> repo default.

    The default filename is ``parrant.policy.yml``; the legacy ``dbt-col-lineage.policy.yml`` is
    still auto-resolved as a fallback so projects that adopted the old name keep working.
    """
    import os

    if path:
        if not os.path.exists(path):
            raise PolicyConfigError(f"policy file not found: '{path}'")
        return path
    for name in ("parrant.policy.yml", "dbt-col-lineage.policy.yml"):
        default = os.path.join(os.getcwd(), name)
        if os.path.exists(default):
            return default
    return None


# --- meta access ------------------------------------------------------------


class MetaLookup(NamedTuple):
    """Result of a meta lookup: whether the key was present, and its value (None when absent)."""

    present: bool
    value: Any


def _dotted_get(container: Any, key: str) -> MetaLookup:
    """Resolve a dotted ``a.b.c`` path against nested dicts. Absent at any hop -> not present."""
    current: Any = container
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MetaLookup(present=False, value=None)
    return MetaLookup(present=True, value=current)


# --- inferred-meta propagation ----------------------------------------------
#
# A column's INFERRED meta for a key is resolved by folding its UPSTREAM lineage, so a
# classification declared once (e.g. ``pii: true`` on a staging column) is inherited by every
# downstream column that derives from it — without re-tagging each one. The fold is:
#   1. the column's OWN declared meta wins (the seed / override / declassification point);
#   2. else combine the upstream source columns' inferred values MOST-RESTRICTIVELY, per a
#      key-specific :class:`CombineStrategy`;
#   3. else (no own meta AND no resolvable upstream) UNKNOWN — surfaced as ``present=False`` so
#      the engine routes it to ``on_missing_meta`` (fail-closed by default), exactly like a
#      missing plain ``meta`` key.


class _Lattice(Enum):
    """A resolved inferred value as a 3-point lattice, ordered least->most restrictive so a
    most-restrictive fold is a plain ``max`` over ``value``: ``LOW < UNKNOWN < HIGH``.

    ``LOW`` = resolvably not-set/falsy; ``HIGH`` = resolvably set/truthy; ``UNKNOWN`` = could not
    be resolved (a root with no meta, or an upstream that folded to UNKNOWN). ``UNKNOWN`` ranks
    ABOVE ``LOW`` because "cannot prove it is not set" is more restrictive than a proven not-set.
    """

    LOW = 0
    UNKNOWN = 1
    HIGH = 2


class CombineStrategy(Protocol):
    """How a single meta key folds its upstream inferred values into one.

    Pluggable per key (see ``_COMBINE_STRATEGIES``) so the fold is not hardcoded to two keys: a
    new key registers its own strategy without touching the engine. ``to_element`` interprets a
    resolved upstream value; ``combine`` folds the per-source elements (an empty list — no
    resolvable upstream — must fold to ``UNKNOWN``).
    """

    def to_element(self, value: Any) -> _Lattice: ...

    def combine(self, elements: List[_Lattice]) -> _Lattice: ...


class _MostRestrictive:
    """PII-style fold: values ordered ``true > unknown > false``. Any upstream truthy => ``HIGH``;
    else any unresolved => ``UNKNOWN``; else ``LOW`` — i.e. the join (``max``) over the lattice."""

    def to_element(self, value: Any) -> _Lattice:
        return _Lattice.HIGH if bool(value) else _Lattice.LOW

    def combine(self, elements: List[_Lattice]) -> _Lattice:
        if not elements:
            return _Lattice.UNKNOWN
        return max(elements, key=lambda element: element.value)


class _BooleanOr:
    """Boolean-OR fold (e.g. ``secret``): any upstream truthy => ``HIGH``; else any unresolved =>
    ``UNKNOWN``; else ``LOW``. Coincides with :class:`_MostRestrictive` for boolean inputs, kept
    as a distinct strategy so the two keys' policies stay independent and a third key can register
    a genuinely different lattice (e.g. a public<internal<confidential ladder) without churn."""

    def to_element(self, value: Any) -> _Lattice:
        return _Lattice.HIGH if bool(value) else _Lattice.LOW

    def combine(self, elements: List[_Lattice]) -> _Lattice:
        if any(element is _Lattice.HIGH for element in elements):
            return _Lattice.HIGH
        if any(element is _Lattice.UNKNOWN for element in elements):
            return _Lattice.UNKNOWN
        return _Lattice.LOW if elements else _Lattice.UNKNOWN


# The fold policy per meta key. Unregistered keys fall back to the most-restrictive fold — the
# fail-safe direction (an unclassified lineage is treated as "not proven safe").
_DEFAULT_STRATEGY: CombineStrategy = _MostRestrictive()
_COMBINE_STRATEGIES: Dict[str, CombineStrategy] = {
    "pii": _MostRestrictive(),
    "secret": _BooleanOr(),
}


def _combine_strategy_for(key: str) -> CombineStrategy:
    """The fold policy for a (dotted) meta key; most-restrictive when none is registered."""
    return _COMBINE_STRATEGIES.get(key.lower(), _DEFAULT_STRATEGY)


def _split_ref(source: str) -> Optional[Tuple[str, str]]:
    """Split a ``model.column`` lineage ref into ``(model, column)``, both lowercased.

    Mirrors the service's split: everything before the last ``.`` is the model, the last segment
    the column. A bare ref (no ``.``, e.g. a literal) is unresolvable -> ``None`` (skipped)."""
    if "." not in source:
        return None
    parts = source.split(".")
    if len(parts) < 2:
        return None
    return (".".join(parts[:-1]).lower(), parts[-1].lower())


def _element_to_lookup(element: _Lattice) -> "MetaLookup":
    """Map a folded lattice element to a :class:`MetaLookup`. ``UNKNOWN`` -> ``present=False`` so
    the engine treats an unresolvable inferred value as a missing key (fail-closed)."""
    if element is _Lattice.HIGH:
        return MetaLookup(present=True, value=True)
    if element is _Lattice.LOW:
        return MetaLookup(present=True, value=False)
    return MetaLookup(present=False, value=None)


class MetaIndex:
    """Read-only accessor for ANY meta key on a model, column, or exposure.

    Wraps a loaded registry (the ``get_model_dbt_meta`` / ``get_column_dbt_meta`` accessors,
    which already return copies and are case-insensitive). Dotted keys resolve against the
    node's merged meta dict. Subject-model+column meta is merged with the **column winning**, so
    a rule may match either a column-level tag (e.g. ``pii``) or a model-level tag.

    Memoizes the per-node meta dict so ``O(rules x changes x reached)`` evaluation stays cheap.

    ``metabase_reach`` (optional) is the cross-boundary index. A reached exposure whose name
    is prefixed ``metabase.dashboard.`` is a Metabase dashboard, whose ``meta`` lives in the
    artifact rather than the dbt manifest; :meth:`exposure_meta` resolves it from this index as
    a fallback so a ``reach.kind: exposure`` predicate can match ``meta.tier`` / ``meta.source``
    on a dashboard with NO new engine surface. Absent -> dbt-only behaviour.
    """

    def __init__(self, registry: Any, metabase_reach: Any = None) -> None:
        self._registry = registry
        self._metabase_reach = metabase_reach
        self._model_cache: Dict[str, Dict[str, Any]] = {}
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self._column_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._exposures: Optional[Dict[str, Any]] = None
        # Memoize resolved inferred meta across the (immutable) column DAG, keyed by
        # (model, column, key) lowercased — folding a diamond visits each node once.
        self._inferred_cache: Dict[Tuple[str, str, str], MetaLookup] = {}

    def _model_dict(self, model: str) -> Dict[str, Any]:
        cached = self._model_cache.get(model)
        if cached is None:
            getter = getattr(self._registry, "get_model_dbt_meta", None)
            cached = dict(getter(model)) if getter is not None else {}
            self._model_cache[model] = cached
        return cached

    def _config_dict(self, model: str) -> Dict[str, Any]:
        cached = self._config_cache.get(model)
        if cached is None:
            getter = getattr(self._registry, "get_model_config", None)
            cached = dict(getter(model)) if getter is not None else {}
            self._config_cache[model] = cached
        return cached

    def _column_dict(self, model: str, column: str) -> Dict[str, Any]:
        key = (model, column)
        cached = self._column_cache.get(key)
        if cached is None:
            getter = getattr(self._registry, "get_column_dbt_meta", None)
            cached = dict(getter(model, column)) if getter is not None else {}
            self._column_cache[key] = cached
        return cached

    def _exposure_dict(self, exposure: str) -> Optional[Any]:
        if self._exposures is None:
            getter = getattr(self._registry, "get_exposures", None)
            try:
                self._exposures = dict(getter()) if getter is not None else {}
            except Exception:
                self._exposures = {}
        return self._exposures.get(exposure)

    def model_meta(self, model: str, key: str) -> MetaLookup:
        return _dotted_get(self._model_dict(model), key)

    def column_meta(self, model: str, column: str, key: str) -> MetaLookup:
        return _dotted_get(self._column_dict(model, column), key)

    def subject_meta(self, model: str, column: str, key: str) -> MetaLookup:
        """Subject meta: the changed column wins over the model (both are 'the subject')."""
        col = self.column_meta(model, column, key)
        if col.present:
            return col
        return self.model_meta(model, key)

    def model_config(self, model: str, key: str) -> MetaLookup:
        """A DOTTED ``config`` key on a model's resolved dbt ``node.config``.

        Model-grained (dbt config is a model-level notion): there is no column merge, unlike
        :meth:`subject_meta`. A missing dotted path is ``present=False`` here; the SET-vs-scalar
        missing-path semantics are applied by :func:`_eval_config`, not stored on the lookup.
        """
        return _dotted_get(self._config_dict(model), key)

    def subject_config(self, model: str, key: str) -> MetaLookup:
        """Subject config: the changed column's *model* config (config is model-grained, so
        there is no column-level fallback — mirrors :meth:`subject_meta` for the call sites)."""
        return self.model_config(model, key)

    def inferred_meta(self, model: str, column: str, key: str) -> MetaLookup:
        """The column's INFERRED meta for ``key``, folded over UPSTREAM lineage.

        Rule 1 — the column's OWN declared (column-level) meta wins, returned *verbatim* (the
        seed / override / declassification point; e.g. a downstream ``pii: false`` stops
        propagation). This is a COLUMN-level notion: a model-level tag does NOT seed or
        declassify ``inferred_meta.*`` — only the column's own meta does. Rule 2 — else fold the
        inferred meta of each upstream source column per the key's :class:`CombineStrategy`
        (most-restrictive). Rule 3 — else, with no own meta and no resolvable upstream, UNKNOWN
        (``present=False``), which the engine routes to ``on_missing_meta`` (fail-closed by
        default), exactly like a missing plain meta key.

        Memoized across the DAG; a cycle guard makes diamonds / recursive refs safe (no infinite
        loop, no double-count). Degrades honestly when the backend exposes no column lineage:
        own meta still resolves, everything else is UNKNOWN.
        """
        strategy = _combine_strategy_for(key)
        result, _ = self._inferred_lookup(model.lower(), column.lower(), key, strategy, set())
        return result

    def _inferred_lookup(
        self,
        model: str,
        column: str,
        key: str,
        strategy: CombineStrategy,
        visiting: Set[Tuple[str, str, str]],
    ) -> Tuple[MetaLookup, bool]:
        """Fold ``model.column``'s inferred value, returning ``(result, touched_cycle)``.

        ``touched_cycle`` is True iff this node's fold DEPENDED on a cycle-guard hit (an upstream
        node that was still in progress on the current recursion stack). Such a result is
        stack-order-dependent — a different entry point into the same SCC would truncate the fold
        elsewhere — so it must NOT be memoized: caching it would poison a later independent query
        for that node, making the answer depend on evaluation order (see ``test_cycle_*``). The
        flag propagates up so every ancestor whose fold consumed a guard hit is likewise left
        uncached. Normal DAG nodes never hit a guard, so the common case still folds each node
        exactly once (the diamond memo test).
        """
        memo_key = (model, column, key)
        cached = self._inferred_cache.get(memo_key)
        if cached is not None:
            # A cached result is fully resolved and independent of the current stack.
            return cached, False
        if memo_key in visiting:
            # Cycle guard: break the loop and signal the caller its fold touched an in-progress
            # node, so the fold that consumes this must not be memoized.
            return MetaLookup(present=False, value=None), True

        # Rule 1: the column's own declared meta wins, verbatim. Independent of lineage, so it is
        # always safe to memoize regardless of any surrounding cycle.
        own = self.column_meta(model, column, key)
        if own.present:
            result = MetaLookup(present=True, value=own.value)
            self._inferred_cache[memo_key] = result
            return result, False

        # Rules 2 & 3: fold the upstream source columns' inferred values.
        visiting.add(memo_key)
        elements: List[_Lattice] = []
        touched_cycle = False
        for src_model, src_column in self._upstream_source_columns(model, column):
            child, child_touched = self._inferred_lookup(
                src_model, src_column, key, strategy, visiting
            )
            touched_cycle = touched_cycle or child_touched
            elements.append(strategy.to_element(child.value) if child.present else _Lattice.UNKNOWN)
        visiting.discard(memo_key)

        result = _element_to_lookup(strategy.combine(elements))
        # Only memoize a fold that did NOT depend on a cycle-guard hit; a guard-truncated fold is
        # order-dependent and possibly wrong, so caching it would make later queries flaky.
        if not touched_cycle:
            self._inferred_cache[memo_key] = result
        return result, touched_cycle

    def _upstream_source_columns(self, model: str, column: str) -> Iterable[Tuple[str, str]]:
        """The distinct ``(model, column)`` upstream source columns feeding ``model.column``.

        Reads the provider's ``get_column_lineage`` edges (duck-typed like the meta accessors, so
        a metadata-only backend degrades to no upstream). Bare, unqualified refs (literals) are
        unresolvable and skipped."""
        getter = getattr(self._registry, "get_column_lineage", None)
        if getter is None:
            return []
        try:
            edges = getter(model, column) or []
        except Exception:
            return []
        seen: Set[Tuple[str, str]] = set()
        out: List[Tuple[str, str]] = []
        for edge in edges:
            for source in getattr(edge, "source_columns", None) or []:
                pair = _split_ref(str(source))
                if pair is not None and pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        return out

    def exposure_meta(self, exposure: str, key: str) -> MetaLookup:
        """Exposure meta plus the reserved fields ``name`` / ``type`` / ``owner``.

        A Metabase dashboard-sourced exposure (``metabase.dashboard.<id>``) resolves its meta
        from the reach index (the artifact carries it, not the manifest) — the architect's
        ``exposure_meta`` fallback. Falls through to the registry for dbt-native exposures.
        """
        if self._metabase_reach is not None and exposure.startswith("metabase.dashboard."):
            dashboard_meta = self._metabase_reach.dashboard_meta(exposure)
            if dashboard_meta is not None:
                return _dotted_get(dashboard_meta, key)
        obj = self._exposure_dict(exposure)
        if obj is None:
            return MetaLookup(present=False, value=None)
        head = key.split(".")[0]
        if head in ("name", "type", "owner"):
            base = getattr(obj, head, None)
            if "." in key:
                return _dotted_get(base, key.split(".", 1)[1])
            return MetaLookup(present=base is not None, value=base)
        meta = getattr(obj, "metadata", None) or {}
        return _dotted_get(meta, key)


# --- reach view -------------------------------------------------------------


class ReachedObject(NamedTuple):
    """One downstream object a change touches, with the mechanism it propagates by."""

    kind: ReachKind
    name: str
    column: Optional[str]
    mechanism: Optional[Mechanism]


def _to_mechanism(raw: Optional[str]) -> Optional[Mechanism]:
    if raw is None:
        return None
    try:
        return Mechanism(raw)
    except ValueError:
        return None


class ImpactView:
    """Per-change reach, derived once from ``get_changeset_impact``'s enriched ``by_change``.

        ``reached(change, kind, mechanism)`` returns the downstream objects a given change touches,
        optionally filtered by mechanism. This is the ONLY reach source the engine reads. A change
        whose impact could not be resolved (``by_change.resolved == False``) carries no reach keys;
    :meth:`is_resolved` reports that so the engine can fail-safe.
    """

    def __init__(self, changeset_impact: Dict[str, Any]) -> None:
        self._by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for entry in changeset_impact.get("by_change", []):
            key = (
                str(entry.get("model")),
                str(entry.get("column")),
                str(entry.get("kind")),
            )
            # Keep the first (resolved) entry for a key; a later duplicate does not clobber it.
            if key not in self._by_key:
                self._by_key[key] = entry

    def _entry(self, change: ColumnChange) -> Optional[Dict[str, Any]]:
        return self._by_key.get((change.model, change.column, change.kind.value))

    def is_resolved(self, change: ColumnChange) -> bool:
        entry = self._entry(change)
        return bool(entry and entry.get("resolved"))

    def reached(
        self,
        change: ColumnChange,
        kind: ReachKind,
        mechanism: Optional[List[Mechanism]] = None,
    ) -> List[ReachedObject]:
        entry = self._entry(change)
        if not entry or not entry.get("resolved"):
            return []
        wanted = set(mechanism) if mechanism else None
        out: List[ReachedObject] = []
        if kind is ReachKind.MODEL:
            for item in entry.get("reached_models", []):
                mech = _to_mechanism(item.get("mechanism"))
                if wanted is not None and mech not in wanted:
                    continue
                out.append(ReachedObject(kind, item["name"], None, mech))
        elif kind is ReachKind.COLUMN:
            for item in entry.get("reached_columns", []):
                mech = _to_mechanism(item.get("mechanism"))
                if wanted is not None and mech not in wanted:
                    continue
                out.append(ReachedObject(kind, item["model"], item.get("column"), mech))
        elif kind is ReachKind.EXPOSURE and wanted is None:
            # Exposures carry no mechanism; a mechanism filter simply matches nothing.
            for item in entry.get("reached_exposures", []):
                out.append(ReachedObject(kind, item["name"], None, None))
        return out


def build_impact_view(changeset_impact: Dict[str, Any]) -> ImpactView:
    """Adapt a ``get_changeset_impact`` report dict into an :class:`ImpactView` (pure)."""
    return ImpactView(changeset_impact)


# --- operator evaluation ----------------------------------------------------


def _as_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return None


def _eval_operator(op: Operator, lookup: MetaLookup, expected: Any) -> Tri:
    """Evaluate one operator against a looked-up value. UNKNOWN on missing/type mismatch.

    Presence/boolean operators are *total* — they answer directly even for an absent key:
      * ``exists`` / ``absent`` answer presence;
      * ``is_true`` == "present AND truthy", ``is_false`` == "present AND falsy" — an absent
        flag is neither, so an untagged column does NOT match ``pii is_true`` (this is what
        keeps the PII rule targeting *PII* columns rather than firing on everything).

    Every *value-comparison* operator (``eq`` / ``in`` / ``subset_of`` / ``matches`` / numeric
    …) on a **missing** key is ``UNKNOWN_MISSING`` and is resolved by ``on_missing_meta`` — this
    is what makes "a reached mart that forgot to declare ``readable_by`` is treated as exposing
    to everyone" block under ``fail_closed``. A value-shape mismatch (e.g. ``subset_of`` on a
    scalar) is a genuine evaluation error and returns ``UNKNOWN_ERROR``, resolved by the separate
    ``on_error`` knob. Never raises.
    """
    if op is Operator.EXISTS:
        return _tri(lookup.present)
    if op is Operator.ABSENT:
        return _tri(not lookup.present)
    if op is Operator.IS_TRUE:
        return _tri(lookup.present and bool(lookup.value))
    if op is Operator.IS_FALSE:
        return _tri(lookup.present and not bool(lookup.value))
    if not lookup.present:
        return Tri.UNKNOWN_MISSING  # missing key -> on_missing_meta

    value = lookup.value
    try:
        if op is Operator.EQ:
            return _tri(value == expected)
        if op is Operator.NE:
            return _tri(value != expected)
        if op is Operator.IN:
            options = _as_list(expected)
            return Tri.UNKNOWN_ERROR if options is None else _tri(value in options)
        if op is Operator.NOT_IN:
            options = _as_list(expected)
            return Tri.UNKNOWN_ERROR if options is None else _tri(value not in options)
        if op is Operator.MATCHES:
            if not isinstance(expected, str):
                return Tri.UNKNOWN_ERROR
            return _tri(re.fullmatch(expected, str(value)) is not None)
        if op in (
            Operator.INTERSECTS,
            Operator.SUBSET_OF,
            Operator.NOT_SUBSET_OF,
            Operator.SUPERSET_OF,
        ):
            left = _as_list(value)
            right = _as_list(expected)
            if left is None or right is None:
                return Tri.UNKNOWN_ERROR  # set op against a non-collection -> type error
            left_set, right_set = set(left), set(right)
            if op is Operator.INTERSECTS:
                return _tri(bool(left_set & right_set))
            if op is Operator.SUBSET_OF:
                return _tri(left_set <= right_set)
            if op is Operator.NOT_SUBSET_OF:
                return _tri(not (left_set <= right_set))
            return _tri(left_set >= right_set)  # SUPERSET_OF
        if op in (Operator.GT, Operator.GE, Operator.LT, Operator.LE):
            if isinstance(value, bool) or isinstance(expected, bool):
                return Tri.UNKNOWN_ERROR
            if not isinstance(value, (int, float)) or not isinstance(expected, (int, float)):
                return Tri.UNKNOWN_ERROR  # numeric op against a non-number -> type error
            if op is Operator.GT:
                return _tri(value > expected)
            if op is Operator.GE:
                return _tri(value >= expected)
            if op is Operator.LT:
                return _tri(value < expected)
            return _tri(value <= expected)  # LE
    except TypeError:
        return Tri.UNKNOWN_ERROR
    return Tri.UNKNOWN_ERROR


# The set-valued operators, whose left operand is a collection. For the ``config`` axis a
# missing dotted path is a proven EMPTY SET for these (see :func:`_eval_config`).
_SET_OPERATORS = frozenset(
    {
        Operator.INTERSECTS,
        Operator.SUBSET_OF,
        Operator.NOT_SUBSET_OF,
        Operator.SUPERSET_OF,
    }
)


def _eval_config(op: Operator, lookup: MetaLookup, expected: Any) -> Tri:
    """Evaluate an operator against a dbt ``config`` (``node.config``) lookup.

    The ``config`` axis mirrors :func:`_eval_operator` (``meta`` semantics) with ONE
    deliberate difference in the *missing dotted path* case, split by operator class:

      * **SET operators** (``subset_of`` / ``not_subset_of`` / ``intersects`` / ``superset_of``):
        an absent path — OR a path present but resolving to ``None`` (e.g. ``grants.select: null``)
        — resolves to the **EMPTY SET** ``[]`` — *present*, NOT unknown. This is the generic,
        correct default: "no ``grants.select`` declared = the empty reader set". So
        ``config.grants.select not_subset_of [allowlist]`` on a model with no grants is
        ``[] ⊄ X == FALSE`` and does NOT fire — a model that grants to nobody cannot over-expose.
        A ``null`` value (readers explicitly nulled out) is treated identically to absent, staying
        consistent with the ``[]`` case rather than routing a genuine "no readers" to a
        fail-closed block via ``UNKNOWN_ERROR``.

      * **SCALAR operators** (``eq`` / ``ne`` / ``matches`` / numeric …): an absent path stays
        ``UNKNOWN_MISSING`` and routes to ``on_missing_meta`` exactly like a missing ``meta`` key.
        The presence/boolean operators (``exists`` / ``absent`` / ``is_true`` / ``is_false``) remain
        *total*, again exactly like ``meta``. A present-``None`` scalar value is left UNCHANGED
        (evaluated as ``None`` by the normal operator semantics) — the empty-set mapping is a
        set-operator-only concern.

    Values are surfaced RAW (as dbt resolved them); the engine never normalizes them.
    """
    if op in _SET_OPERATORS and (not lookup.present or lookup.value is None):
        # Absent path OR an explicit ``null`` collapse to the proven empty set for set ops.
        lookup = MetaLookup(present=True, value=[])
    return _eval_operator(op, lookup, expected)


def _eval_inferred(op: Operator, lookup: MetaLookup, expected: Any) -> Tri:
    """Evaluate an operator against an INFERRED-meta lookup.

    Identical to :func:`_eval_operator` once the value is resolved, but an UNRESOLVED inferred
    value (``present=False``) is ``UNKNOWN_MISSING`` for EVERY operator — including the otherwise
    "total" ``is_true`` / ``is_false`` / ``exists``. This is the whole point of the ``inferred_meta.*``
    namespace: a classification that cannot be proven along the lineage must route to
    ``on_missing_meta`` (fail-closed) rather than silently read as ``False``. A resolved value
    (own meta, or a folded ``true`` / ``false``) is evaluated by the normal operator semantics."""
    if not lookup.present:
        return Tri.UNKNOWN_MISSING
    return _eval_operator(op, lookup, expected)


# --- the engine -------------------------------------------------------------


class _Trace:
    """Accumulates matched reach object names while a predicate is evaluated for one subject."""

    def __init__(self) -> None:
        self.matched_reach: List[str] = []
        self.saw_unresolved_reach: bool = False
        # Compact ``axis.key`` labels of the leaf conditions that resolved UNKNOWN — carried
        # into the suppressed-unknown (``unproven``) telemetry so a report can say WHICH
        # condition could not be proven, not just that one exists.
        self.unknown_leaves: List[str] = []

    def add(self, name: str) -> None:
        if name not in self.matched_reach:
            self.matched_reach.append(name)

    def note_unknown_leaf(self, label: str) -> None:
        if label not in self.unknown_leaves:
            self.unknown_leaves.append(label)


class PolicyEngine:
    """Evaluate a Policy against a changeset and its impact/meta context.

    Stateless across ``evaluate`` calls; construction binds the read-only indexes.
    """

    def __init__(
        self,
        policy: Policy,
        meta: MetaIndex,
        impact: ImpactView,
        breaks: List[BreakFinding],
    ) -> None:
        self._policy = policy
        self._meta = meta
        self._impact = impact
        self._breaks = breaks
        # Index provable breaks by (model, column) lowercased for O(1) structural lookups.
        self._break_keys = {(b.change_model.lower(), b.change_column.lower()) for b in breaks}

    # -- public API ----------------------------------------------------------

    def evaluate(self, changes: List[ColumnChange]) -> PolicyVerdict:
        """Run every rule against every subject (or once, for aggregate rules); combine per §2.6.

        Total and deterministic: never raises on rule content (config errors surface at load).
        An undecidable predicate resolves per the rule's ``MissingMetaPolicy``.
        """
        # index the changeset by (model, column) lowercased so override caps are O(1).
        self._change_by_key: Dict[Tuple[str, str], ColumnChange] = {
            (c.model.lower(), c.column.lower()): c for c in changes
        }

        hits: List[RuleHit] = []
        build_set: set[str] = set()
        test_set: set[str] = set()
        notifications: List[Notification] = []
        unproven: List[UnprovenPolicyHit] = []
        skipped = 0
        unresolved_reach = 0

        for rule in self._policy.rules:
            subjects: List[Optional[ColumnChange]]
            subjects = [None] if rule.scope == "aggregate" else list(changes)
            for subject in subjects:
                trace = _Trace()
                if rule.scope == "aggregate":
                    result = self._eval_aggregate(rule.predicate, changes, trace)
                else:
                    assert subject is not None
                    result = self._eval_predicate(rule.predicate, subject, trace)
                if trace.saw_unresolved_reach:
                    unresolved_reach += 1

                resolved = self._resolve(result, rule)
                if resolved is None:  # skip policy
                    if _is_unknown(result):
                        skipped += 1
                    continue
                if not resolved:
                    # Suppressed-unknown telemetry (issue #124): a fail_closed knob WOULD have
                    # fired this rule were it blocking, but the rule is non-blocking (and the
                    # warn_rules_fire_on_unknown knob is off), so it was suppressed. Record the
                    # suppression so an all-warn pilot policy never silently understates what
                    # block mode will do. Report-only: no hit, no decision, no exit-code change.
                    if (
                        _is_unknown(result)
                        and not _is_blocking(rule)
                        and self._unknown_policy(result, rule) is MissingMetaPolicy.FAIL_CLOSED
                    ):
                        unproven.append(
                            UnprovenPolicyHit(
                                rule_id=rule.id,
                                change_model=subject.model if subject else None,
                                change_column=subject.column if subject else None,
                                unknown_cause=(
                                    "error" if result is Tri.UNKNOWN_ERROR else "missing"
                                ),
                                unknown_leaves=list(trace.unknown_leaves),
                            )
                        )
                    continue

                hit, b_add, t_add, notes = self._apply_actions(rule, subject, trace)
                # mark a hit that fired via a fail-safe UNKNOWN resolution (a blocking rule
                # firing on an undecidable predicate under fail_closed) rather than a proven TRUE
                # match — the headline trust signal in ``policy test``. Applies identically to
                # change- and aggregate-scope rules; semantic-default hits (added below) and
                # override caps leave it False.
                if _is_unknown(result):
                    hit.fired_on_unknown = True
                    hit.unknown_cause = "error" if result is Tri.UNKNOWN_ERROR else "missing"
                hits.append(hit)
                build_set |= b_add
                test_set |= t_add
                notifications.extend(notes)

        hits.extend(self._semantic_default_hits(changes))

        # apply override pragmas as a post-evaluation cap on each hit's contribution,
        # BEFORE combining — so ``_combine_decision`` naturally uses the capped decisions.
        self._apply_override_caps(hits)

        decision = self._combine_decision(hits)
        deduped_notes = _dedup_notifications(notifications)
        return PolicyVerdict(
            decision=decision,
            hits=hits,
            build_set=sorted(build_set),
            test_set=sorted(test_set),
            notifications=deduped_notes,
            evaluated_rules=len(self._policy.rules),
            fired_rules=len(hits),
            unresolved_reach_count=unresolved_reach,
            skipped_missing_meta=skipped,
            unproven=unproven,
            unproven_count=len(unproven),
        )

    # -- built-in semantic-severity knobs ------------------------------------

    def _semantic_default_hits(self, changes: List[ColumnChange]) -> List[RuleHit]:
        """Synthesize gate contributions from ``defaults.on_meaning_changed`` / ``on_indeterminate``.

        These are the ergonomic shortcut for "gate on the semantic axis" without authoring a
        ``change.semantic``/``change.breaking`` rule: each changed column whose classification
        is ``MEANING_CHANGED`` or ``INDETERMINATE`` contributes the decision its knob names.
        The two are independent — a user can block one and warn the other — which is the whole
        point of splitting the axis.

        Scope mirrors the no-policy verdict (verdict.py): only a change that actually carries a
        semantic classification participates. Structural add/remove/type changes leave
        ``semantic`` unset and are governed by provable breaks / user rules, not this axis; a
        proven ``EQUIVALENT`` (never emitted as a change anyway) contributes nothing. An unset
        knob, or one set to ``allow``, adds no hit, so behavior is unchanged by default.
        """
        defaults = self._policy.defaults
        by_kind = {
            SemanticChangeKind.MEANING_CHANGED: (defaults.on_meaning_changed, "on_meaning_changed"),
            SemanticChangeKind.INDETERMINATE: (defaults.on_indeterminate, "on_indeterminate"),
        }
        hits: List[RuleHit] = []
        for change in changes:
            if change.semantic is None:
                continue
            decision, knob = by_kind.get(change.semantic, (None, ""))
            if decision is None or decision is GateDecision.ALLOW:
                continue
            hits.append(
                RuleHit(
                    rule_id=f"builtin:{knob}",
                    decision=decision,
                    change_model=change.model,
                    change_column=change.column,
                    actions=[],
                )
            )
        return hits

    # -- fail-safe resolution ------------------------------------------------

    def _resolve(self, result: Tri, rule: Rule) -> Optional[bool]:
        """Resolve a (possibly UNKNOWN) predicate result to fire / not-fire / skip.

        TRUE -> fire; FALSE -> not fire. An UNKNOWN is routed to the fail-safe knob that matches
        its *cause* — the two knobs are independent:
          * ``UNKNOWN_MISSING`` (missing meta / unresolved reach) -> ``on_missing_meta``;
          * ``UNKNOWN_ERROR`` (operator/type mismatch) -> ``on_error``.
        Each falls back rule -> policy default. The chosen policy then resolves the leaf:
          * fail_closed: a *blocking* rule fires (bias toward safety); a non-blocking rule does
            not (never manufacture a spurious warning) — the asymmetry from — UNLESS the
            policy-level ``defaults.warn_rules_fire_on_unknown`` knob is on, in which case the
            non-blocking rule fires too, at its own declared severity (issue #124). A suppressed
            non-blocking firing (knob off) is surfaced as ``PolicyVerdict.unproven`` telemetry.
          * fail_open: never fires on UNKNOWN.
          * skip: returns None (drop the rule for this subject; counted for honesty).
        """
        if result is Tri.TRUE:
            return True
        if result is Tri.FALSE:
            return False
        policy = self._unknown_policy(result, rule)
        if policy is MissingMetaPolicy.SKIP:
            return None
        if policy is MissingMetaPolicy.FAIL_OPEN:
            return False
        # fail_closed: blocking rules fire on UNKNOWN; non-blocking rules only under the knob.
        return _is_blocking(rule) or self._policy.defaults.warn_rules_fire_on_unknown

    def _unknown_policy(self, result: Tri, rule: Rule) -> MissingMetaPolicy:
        """The fail-safe knob that governs this UNKNOWN, per its cause (rule -> policy default)."""
        if result is Tri.UNKNOWN_ERROR:
            return rule.on_error or self._policy.defaults.on_error
        return rule.on_missing_meta or self._policy.defaults.on_missing_meta

    # -- predicate evaluation (change scope) ---------------------------------

    def _eval_predicate(self, predicate: Predicate, subject: ColumnChange, trace: _Trace) -> Tri:
        if predicate.all_ is not None:
            return _and([self._eval_predicate(p, subject, trace) for p in predicate.all_])
        if predicate.any_ is not None:
            return _or([self._eval_predicate(p, subject, trace) for p in predicate.any_])
        if predicate.not_ is not None:
            return _not(self._eval_predicate(predicate.not_, subject, trace))
        # A leaf: evaluate it and, when it resolves UNKNOWN, note its compact ``axis.key`` label
        # on the trace so the suppressed-unknown telemetry can name WHICH condition was unproven.
        result: Tri
        label: str
        if predicate.change is not None:
            result = self._eval_change(predicate.change, subject)
            label = f"change.{predicate.change.field}"
        elif predicate.meta is not None:
            result = self._eval_meta_subject(predicate.meta, subject)
            label = f"meta.{predicate.meta.key}"
        elif predicate.inferred_meta is not None:
            result = self._eval_inferred_subject(predicate.inferred_meta, subject)
            label = f"inferred_meta.{predicate.inferred_meta.key}"
        elif predicate.config is not None:
            result = self._eval_config_subject(predicate.config, subject)
            label = f"config.{predicate.config.key}"
        elif predicate.reach is not None:
            result = self._eval_reach(predicate.reach, subject, trace)
            label = f"reach.{predicate.reach.kind.value}"
        elif predicate.structural is not None:
            result = self._eval_structural(predicate.structural, subject, trace)
            label = f"structural.{predicate.structural.fact}"
        else:
            return Tri.UNKNOWN_ERROR  # unreachable: the schema enforces exactly-one
        if _is_unknown(result):
            trace.note_unknown_leaf(label)
        return result

    def _eval_change(self, cond: ChangeCondition, subject: ColumnChange) -> Tri:
        # NOTE: ``change.semantic`` (and ``change.kind``) are always PRESENT lookups, so a
        # comparison over them is never fail-safe over the semantic axis: an INDETERMINATE /
        # None semantic surfaces as the literal ``"indeterminate"`` string, so
        # ``change.semantic eq meaning_changed`` is simply FALSE for it (no UNKNOWN, no
        # fail-safe bias). Consumers that want to catch "might be breaking" must gate on
        # ``change.breaking is_true`` (which folds INDETERMINATE/None into breaking) rather
        # than equality on the semantic label.
        semantic = subject.semantic or SemanticChangeKind.INDETERMINATE
        if cond.field == "kind":
            lookup = MetaLookup(True, subject.kind.value)
        elif cond.field == "semantic":
            lookup = MetaLookup(True, semantic.value)
        elif cond.field == "breaking":
            lookup = MetaLookup(True, semantic.is_breaking)
        elif cond.field == "model":
            lookup = MetaLookup(True, subject.model)
        else:  # column
            lookup = MetaLookup(True, subject.column)
        return _eval_operator(cond.op, lookup, cond.value)

    def _eval_meta_subject(self, cond: MetaCondition, subject: ColumnChange) -> Tri:
        lookup = self._meta.subject_meta(subject.model, subject.column, cond.key)
        return _eval_operator(cond.op, lookup, cond.value)

    def _eval_inferred_subject(self, cond: MetaCondition, subject: ColumnChange) -> Tri:
        """``inferred_meta.<key>`` on the subject: resolve via upstream-folding ``inferred_meta``
        rather than the subject's own declared meta (the ``meta.*`` path). Additive — ``meta.*``
        is untouched."""
        lookup = self._meta.inferred_meta(subject.model, subject.column, cond.key)
        return _eval_inferred(cond.op, lookup, cond.value)

    def _eval_config_subject(self, cond: MetaCondition, subject: ColumnChange) -> Tri:
        """``config.<dotted.key>`` on the subject's model: resolve against the model's dbt
        ``node.config`` (``grants.select``, ``materialized``, ``tags`` …). Model-grained.
        A missing dotted path is the EMPTY SET for set operators and UNKNOWN for scalar ones —
        see :func:`_eval_config`. Additive: ``meta.*`` / ``inferred_meta.*`` are untouched."""
        lookup = self._meta.subject_config(subject.model, cond.key)
        return _eval_config(cond.op, lookup, cond.value)

    def _eval_reach(self, cond: ReachCondition, subject: ColumnChange, trace: _Trace) -> Tri:
        if not self._impact.is_resolved(subject):
            # Cannot prove it does NOT reach a matching node -> unresolved reach is a MISSING
            # cause, resolved by on_missing_meta (fail-safe §7.4).
            trace.saw_unresolved_reach = True
            return Tri.UNKNOWN_MISSING
        objects = self._impact.reached(subject, cond.kind, cond.mechanism)
        n_true = 0
        unknowns: List[Tri] = []
        for obj in objects:
            inner = self._eval_where(cond.where, obj)
            if inner is Tri.TRUE:
                # Only an object that PROVABLY satisfies the `where` is a match. It is what
                # populates matched_reach and {reach.count}; an UNKNOWN object (e.g. a reached
                # exposure missing the meta key the `where` tests) is NOT reported as matched,
                # even when it lets the rule fire under a fail-safe knob. This keeps the reported
                # reach precise without touching the block/allow decision below.
                n_true += 1
                trace.add(_reached_display(obj))
            elif _is_unknown(inner):
                unknowns.append(inner)
        if n_true >= cond.min_count:
            return Tri.TRUE
        if n_true + len(unknowns) >= cond.min_count:
            # Undecidable: propagate the cause of the reached objects that might still match.
            return _merge_unknown(unknowns)
        return Tri.FALSE

    def _eval_where(self, predicate: Predicate, obj: ReachedObject) -> Tri:
        """Evaluate a reach's inner ``where`` against one reached object (matches its meta.*)."""
        if predicate.all_ is not None:
            return _and([self._eval_where(p, obj) for p in predicate.all_])
        if predicate.any_ is not None:
            return _or([self._eval_where(p, obj) for p in predicate.any_])
        if predicate.not_ is not None:
            return _not(self._eval_where(predicate.not_, obj))
        if predicate.meta is not None:
            return _eval_operator(
                predicate.meta.op, self._reached_meta(obj, predicate.meta.key), predicate.meta.value
            )
        if predicate.inferred_meta is not None:
            return _eval_inferred(
                predicate.inferred_meta.op,
                self._reached_inferred(obj, predicate.inferred_meta.key),
                predicate.inferred_meta.value,
            )
        if predicate.config is not None:
            return _eval_config(
                predicate.config.op,
                self._reached_config(obj, predicate.config.key),
                predicate.config.value,
            )
        # change / reach / structural are not meaningful against a reached object -> error cause.
        return Tri.UNKNOWN_ERROR

    def _reached_meta(self, obj: ReachedObject, key: str) -> MetaLookup:
        if obj.kind is ReachKind.MODEL:
            return self._meta.model_meta(obj.name, key)
        if obj.kind is ReachKind.COLUMN:
            return self._meta.column_meta(obj.name, obj.column or "", key)
        return self._meta.exposure_meta(obj.name, key)

    def _reached_config(self, obj: ReachedObject, key: str) -> MetaLookup:
        """``config.<key>`` on a reached object. Config is model-grained, so a reached MODEL or
        COLUMN resolves it on that node's model (both carry the model in ``obj.name``); a reached
        exposure has no dbt ``config`` and is ``present=False`` -> the set-op empty-set / scalar
        UNKNOWN semantics of :func:`_eval_config` then apply."""
        if obj.kind is ReachKind.MODEL or obj.kind is ReachKind.COLUMN:
            return self._meta.model_config(obj.name, key)
        return MetaLookup(present=False, value=None)

    def _reached_inferred(self, obj: ReachedObject, key: str) -> MetaLookup:
        """``inferred_meta.<key>`` on a reached object. Inferred meta is a COLUMN-level notion, so
        only a reached *column* resolves; a reached model / exposure has no column DAG to fold and
        is UNKNOWN (``present=False``) -> fail-safe, never a spurious match."""
        if obj.kind is ReachKind.COLUMN and obj.column:
            return self._meta.inferred_meta(obj.name, obj.column, key)
        return MetaLookup(present=False, value=None)

    def _eval_structural(
        self, cond: StructuralCondition, subject: ColumnChange, trace: _Trace
    ) -> Tri:
        if cond.fact == "provable_test_break":
            return _tri((subject.model.lower(), subject.column.lower()) in self._break_keys)
        if not self._impact.is_resolved(subject):
            trace.saw_unresolved_reach = True
            return Tri.UNKNOWN_MISSING  # unresolved reach -> on_missing_meta
        if cond.fact == "touches_exposure":
            return _tri(bool(self._impact.reached(subject, ReachKind.EXPOSURE)))
        # reaches_anything
        reaches = (
            self._impact.reached(subject, ReachKind.MODEL)
            or self._impact.reached(subject, ReachKind.COLUMN)
            or self._impact.reached(subject, ReachKind.EXPOSURE)
        )
        return _tri(bool(reaches))

    # -- predicate evaluation (aggregate scope) ------------------------------

    def _eval_aggregate(
        self, predicate: Predicate, changes: List[ColumnChange], trace: _Trace
    ) -> Tri:
        """Aggregate scope: each leaf is quantified existentially over the whole changeset.

        Suitable for project-wide, single-axis rules ("any provable break anywhere -> block").
        Combinators combine the per-leaf existential results with Kleene logic.
        """
        if predicate.all_ is not None:
            return _and([self._eval_aggregate(p, changes, trace) for p in predicate.all_])
        if predicate.any_ is not None:
            return _or([self._eval_aggregate(p, changes, trace) for p in predicate.any_])
        if predicate.not_ is not None:
            return _not(self._eval_aggregate(predicate.not_, changes, trace))
        # A leaf: TRUE if any change satisfies it, else UNKNOWN if any is undecided, else FALSE.
        return _or([self._eval_predicate(predicate, change, trace) for change in changes])

    # -- actions -------------------------------------------------------------

    def _apply_actions(
        self, rule: Rule, subject: Optional[ColumnChange], trace: _Trace
    ) -> Tuple[RuleHit, "set[str]", "set[str]", List[Notification]]:
        build_add: set[str] = set()
        test_add: set[str] = set()
        notes: List[Notification] = []
        action_kinds: List[ActionKind] = []

        for action in rule.action:
            action_kinds.append(action.type)
            if action.type is ActionKind.ADD_TO_BUILD_SET:
                build_add |= self._collect_nodes(action, subject)
            elif action.type is ActionKind.ADD_TO_TEST_SET:
                test_add |= self._collect_nodes(action, subject)
            elif action.type is ActionKind.NOTIFY:
                notes.append(self._build_notification(rule, action, subject, trace))

        return (
            RuleHit(
                rule_id=rule.id,
                decision=_rule_decision(rule),
                change_model=subject.model if subject else None,
                change_column=subject.column if subject else None,
                matched_reach=trace.matched_reach[:_MATCHED_REACH_NAME_CAP],
                actions=action_kinds,
            ),
            build_add,
            test_add,
            notes,
        )

    def _collect_nodes(self, action: Action, subject: Optional[ColumnChange]) -> "set[str]":
        nodes: set[str] = set()
        if subject is None:
            return nodes
        if action.include in ("reached", "both"):
            for obj in self._impact.reached(subject, ReachKind.MODEL, action.mechanism):
                nodes.add(obj.name)
        if action.include in ("subject", "both"):
            nodes.add(subject.model)
        return nodes

    def _build_notification(
        self, rule: Rule, action: Action, subject: Optional[ColumnChange], trace: _Trace
    ) -> Notification:
        template = action.message or ""
        values = {
            "change.model": subject.model if subject else "",
            "change.column": subject.column if subject else "",
            "reach.count": str(len(trace.matched_reach)),
            "rule.id": rule.id,
        }
        message = _interpolate(template, values)
        return Notification(
            channel=action.channel or "",
            target=action.target or "",
            message=message,
        )

    # -- override caps --------------------------------------------------

    def _apply_override_caps(self, hits: List[RuleHit]) -> None:
        """Cap each subject-scoped hit whose change carries an override (mutates in place).

        - ``allow-break`` caps a BLOCK to WARN (the only verb that may touch a block).
        - ``allow-change`` caps a WARN/BLOCK to ALLOW, EXCEPT it must NOT silence a BLOCK on a
          change that is itself a provable break — only ``allow-break`` can, and only to WARN.

        The provable-break guard is approximated as "hit.decision == BLOCK AND (model, column)
        in break_keys". This over-protects (an unrelated non-break BLOCK on a change that also
        happens to be a break stays armed under allow-change), which errs toward keeping the
        gate armed — the intended fail-safe direction. Aggregate-scope hits (no subject) are
        never capped.
        """
        for hit in hits:
            if hit.change_model is None or hit.change_column is None:
                continue  # aggregate-scope hit: never capped
            change = self._change_by_key.get((hit.change_model.lower(), hit.change_column.lower()))
            if change is None or change.override is None:
                continue
            override = change.override
            is_break = (
                hit.change_model.lower(),
                hit.change_column.lower(),
            ) in self._break_keys
            if override.verb is OverrideVerb.ALLOW_BREAK:
                if hit.decision is GateDecision.BLOCK:
                    hit.original_decision = hit.decision
                    hit.decision = GateDecision.WARN
                    hit.overridden = True
                    hit.override_reason = override.reason
            else:  # allow-change (soft): cannot silence a provable break's block
                if is_break and hit.decision is GateDecision.BLOCK:
                    continue
                if hit.decision.severity > GateDecision.ALLOW.severity:
                    hit.original_decision = hit.decision
                    hit.decision = GateDecision.ALLOW
                    hit.overridden = True
                    hit.override_reason = override.reason

    # -- combination ---------------------------------------------------------

    def _combine_decision(self, hits: List[RuleHit]) -> GateDecision:
        decision = GateDecision.ALLOW
        for hit in hits:
            if hit.decision.severity > decision.severity:
                decision = hit.decision
        return decision


# --- helpers ----------------------------------------------------------------


def _is_blocking(rule: Rule) -> bool:
    return any(a.type is ActionKind.BLOCK for a in rule.action)


def _rule_decision(rule: Rule) -> GateDecision:
    """The strongest gate contribution a rule's action set makes."""
    if any(a.type is ActionKind.BLOCK for a in rule.action):
        return GateDecision.BLOCK
    if any(a.type is ActionKind.WARN for a in rule.action):
        return GateDecision.WARN
    return GateDecision.ALLOW


def _reached_display(obj: ReachedObject) -> str:
    if obj.kind is ReachKind.COLUMN and obj.column:
        return f"{obj.name}.{obj.column}"
    return obj.name


_INTERPOLATE_RE = re.compile(r"\{([a-z]+(?:\.[a-z]+)?)\}")


def _interpolate(template: str, values: Dict[str, str]) -> str:
    """Substitute the small safe vocabulary ``{change.*}`` / ``{reach.count}`` / ``{rule.id}``.

    Unknown tokens are left verbatim (no arbitrary code, no KeyError).
    """

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        return values.get(token, match.group(0))

    return _INTERPOLATE_RE.sub(repl, template)


def _dedup_notifications(notes: List[Notification]) -> List[Notification]:
    seen: set[Tuple[str, str, str]] = set()
    out: List[Notification] = []
    for note in notes:
        key = (note.channel, note.target, note.message)
        if key not in seen:
            seen.add(key)
            out.append(note)
    return out


# --- top-level convenience (for wiring) ----------------------------------


def applied_policy_overrides(
    verdict: PolicyVerdict, changes: List[ColumnChange]
) -> List[Dict[str, Any]]:
    """Honored-override records derived from capped policy hits, in the SAME shape the default
    gate emits (``applied_overrides``). Cross-references ``ColumnChange.override`` for the verb /
    source_line / scope that ``RuleHit`` does not carry, so both report paths are uniform."""
    by_key = {(c.model.lower(), c.column.lower()): c for c in changes}
    records: List[Dict[str, Any]] = []
    for hit in verdict.hits:
        if not hit.overridden:
            continue
        change = by_key.get((str(hit.change_model).lower(), str(hit.change_column).lower()))
        override = change.override if change is not None else None
        records.append(
            {
                "model": hit.change_model,
                "column": hit.change_column,
                "verb": override.verb.value if override else "",
                "reason": hit.override_reason or (override.reason if override else ""),
                "downgraded_from": (hit.original_decision.value if hit.original_decision else None),
                "downgraded_to": hit.decision.value,
                "source_line": override.source_line if override else None,
                "scope": override.scope if override else None,
                "rule_id": hit.rule_id,
            }
        )
    return records


def ineffective_policy_overrides(
    verdict: PolicyVerdict,
    changes: List[ColumnChange],
    breaks: Optional[List[BreakFinding]] = None,
) -> List[Dict[str, Any]]:
    """Override records that landed on a real changed column but capped NO hit — surfaced so an
    ineffective pragma (e.g. allow-change on a break, or an override where no rule fired) is
    never silently ignored. Same shape as the default gate's ``ineffective_overrides``."""
    break_keys = {(b.change_model.lower(), b.change_column.lower()) for b in (breaks or [])}
    capped = {
        (str(h.change_model).lower(), str(h.change_column).lower())
        for h in verdict.hits
        if h.overridden
    }
    records: List[Dict[str, Any]] = []
    for change in changes:
        if change.override is None:
            continue
        key = (change.model.lower(), change.column.lower())
        if key in capped:
            continue  # effective
        records.append(ineffective_override_record(change, key in break_keys))
    return records


def evaluate_policy(
    changes: List[ColumnChange],
    changeset_impact: Dict[str, Any],
    registry: Any,
    policy: Policy,
    breaks: Optional[List[BreakFinding]] = None,
    metabase_reach: Any = None,
) -> PolicyVerdict:
    """One-call helper can wire into ``cli/main.py``: build the indexes and evaluate.

    Given the changeset, the ``get_changeset_impact`` report dict, a loaded registry, and a
    parsed :class:`Policy`, returns the :class:`PolicyVerdict`. ``breaks`` should be the output
    of ``classify_provable_breaks`` (the structural signal); omitted -> no provable breaks.
    ``metabase_reach`` (optional,) lets ``reach.kind: exposure`` rules resolve a reached
    Metabase dashboard's ``meta`` from the artifact; omitted -> dbt-only reach.
    """
    meta = MetaIndex(registry, metabase_reach=metabase_reach)
    view = build_impact_view(changeset_impact)
    engine = PolicyEngine(policy, meta, view, breaks or [])
    return engine.evaluate(changes)


__all__ = [
    "ImpactView",
    "MetaIndex",
    "MetaLookup",
    "PolicyConfigError",
    "PolicyEngine",
    "ReachedObject",
    "applied_policy_overrides",
    "build_impact_view",
    "evaluate_policy",
    "ineffective_policy_overrides",
    "load_policy",
    "parse_policy",
]
