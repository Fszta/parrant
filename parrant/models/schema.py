from enum import Enum

from pydantic import BaseModel, Field, ConfigDict, model_validator
from typing import List, Optional, Set, Dict, Literal, Any


class OverrideVerb(str, Enum):
    """The two override pragma verbs, deliberately distinct so a soft acknowledgement can
    NEVER silence a hard break (see override fail-safe invariants).

    - ``ALLOW_CHANGE`` (soft): downgrades a REVIEW/WARN contribution for a column to allow.
    - ``ALLOW_BREAK`` (hard): the ONLY verb that can downgrade a *provable BLOCK*, and only
      to REVIEW/WARN — never to safe/allow.
    """

    ALLOW_CHANGE = "allow-change"
    ALLOW_BREAK = "allow-break"

    @property
    def is_hard(self) -> bool:
        """True for the hard ``allow-break`` verb (the only one that can touch a break)."""
        return self is OverrideVerb.ALLOW_BREAK


class OverrideDirective(BaseModel):
    """One parsed ``-- lineage:allow-(change|break) ...`` pragma from a model's head SQL.

    Lives in ``models.schema`` (not ``changeset.py``) so ``parser/sql_parser.py`` — which sits
    BELOW ``lineage`` in the layering — can import it without inverting the architecture.
    ``reason`` is guaranteed non-empty by the parser: a reasonless pragma is dropped as a loud
    warning and never constructed (the whole audit value is the justification).
    """

    verb: OverrideVerb
    # Lowercased target column. ``None`` => model scope OR an unresolved line-adjacency.
    column: Optional[str] = None
    reason: str
    # ``model`` when no column arg and the pragma precedes the first SELECT; ``column`` otherwise.
    scope: Literal["column", "model"]
    # 1-indexed line within the scanned (compiled) head SQL — NOTE: compiled-relative.
    source_line: int
    # Set by the changeset builder once it knows which model this SQL belongs to.
    model: Optional[str] = None

    def to_record(self) -> Dict[str, Any]:
        """The report dict skeleton for an override record (stale / applied / ineffective)."""
        return {
            "model": self.model,
            "column": self.column,
            "verb": self.verb.value,
            "reason": self.reason,
            "source_line": self.source_line,
            "scope": self.scope,
        }


class SemanticChangeKind(str, Enum):
    """Semantic relationship between a column's base and head defining expression.

    Maps to the roadmap's breaking / non-breaking axis (orthogonal to ``ChangeKind``):
      - ``EQUIVALENT``      → non-breaking (cosmetic-only; the column is NOT emitted as changed)
      - ``MEANING_CHANGED`` → breaking (the expression's meaning changed)
      - ``INDETERMINATE``   → conservative-breaking (could not parse/compare → fail safe)
    """

    EQUIVALENT = "equivalent"
    MEANING_CHANGED = "meaning_changed"
    INDETERMINATE = "indeterminate"

    @property
    def is_breaking(self) -> bool:
        """Everything except a proven ``EQUIVALENT`` is treated as breaking (fail-safe)."""
        return self is not SemanticChangeKind.EQUIVALENT


class SemanticDiff(BaseModel):
    """Result of comparing two SQL expressions for semantic equality.

    ``equal`` is the fast boolean the changeset uses to decide whether to emit a column;
    ``kind`` is the roadmap classification; ``reason`` is a short human string for display.
    """

    equal: bool
    kind: SemanticChangeKind
    reason: str


class ColumnLineage(BaseModel):
    source_columns: Set[str]
    transformation_type: Literal["direct", "renamed", "derived"]
    sql_expression: Optional[str] = None
    description: Optional[str] = None


class UnresolvedColumnEdge(BaseModel):
    """A column whose upstream source parrant could NOT genuinely resolve.

    Emitted instead of fabricating a confident-but-wrong source column, so that the
    resolution/confidence pass can degrade the model out of the ``catalog_backed``-clean
    partition and drop its downstream ``confidence.level`` to ``partial`` — see
    ``UNRESOLVED_EDGES_PLAN.md`` §4a for the contract.

    Fail-safe: a marker only ADDS information. It never removes a legitimately-resolved edge and
    never raises confidence; its presence can only lower confidence / widen the rebuild set.

    ``model`` is the dbt node this marker belongs to. The **parser** cannot know its own model
    name (it only sees SQL), so it emits markers with ``model == ""``; the **registry** stamps the
    real model name when it attaches the marker set to ``model.metadata["unresolved_edges"]``.
    ``column`` is the model's own output column (lowercased) whose source is unresolved.
    ``reason`` is the construct that defeated resolution; ``detail`` carries the raw fabricated
    token that was dropped (e.g. ``"p.value"``) purely for the report.
    """

    model: str = ""
    column: str
    reason: Literal[
        "phantom_alias",
        "unexpandable_star",
        "fabricated_column",
        "star_rename",
        "pivot_output",
        "other",
    ]
    detail: Optional[str] = None


class Column(BaseModel):
    name: str
    model_name: str
    description: Optional[str] = None
    data_type: Optional[str] = None
    lineage: Optional[List[ColumnLineage]] = Field(default_factory=list)  # type: ignore
    metadata: Optional[Dict[str, Any]] = None

    @property
    def full_name(self) -> str:
        return f"{self.model_name}.{self.name}"


class Exposure(BaseModel):
    name: str
    type: str
    url: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[Dict[str, Any]] = None
    unique_id: str
    depends_on_models: Set[str] = Field(default_factory=set)
    resource_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class TestNode(BaseModel):
    """A dbt test node, read from ``manifest.json`` (``resource_type == "test"``).

    We never execute the test — we read what it *declares*. A ``not_null`` test on
    ``orders.customer_id`` is a declaration whose target model is ``orders`` and whose
    target column is ``customer_id``. This is the raw material for statically proving
    that a removed/renamed column breaks a test on the next ``dbt build``.
    """

    # Tell pytest this is not a test class (the name starts with "Test").
    __test__ = False

    unique_id: str
    # ``test_metadata.name``: not_null / unique / relationships / accepted_values / ...
    test_name: str
    # The model the test is attached to (lowercased). ``None`` when it cannot be
    # attributed honestly (e.g. a singular/custom test with no clear model).
    target_model: Optional[str] = None
    # The column under test (lowercased). ``None`` for tests with no column (e.g.
    # a model-level test) — never guessed.
    target_column: Optional[str] = None
    # For ``relationships`` tests: the referenced ("parent") side. ``referenced_model``
    # comes from ``kwargs.to`` (a ``ref(...)``), ``referenced_column`` from ``kwargs.field``.
    referenced_model: Optional[str] = None
    referenced_column: Optional[str] = None
    # ``original_file_path`` — the ``file:line`` the reviewer would open to fix it.
    resource_path: Optional[str] = None


class BreakFinding(BaseModel):
    """A *provable*, offline-verifiable breakage a column change causes on the next build.

    Unlike heuristic severity ("this reaches N models"), a break finding names a concrete
    dbt object that will fail: a test whose target column no longer exists. It is the only
    class of impact objective enough to *block* a PR — see the verdict classifier.
    """

    # ``break_test``: a dbt test references a column the change removed/renamed, so it can
    # no longer compile. (``break_ref`` — a downstream model that still selects a removed
    # column — is deferred; it needs a head-side consumers index and same-PR-fix handling.)
    break_kind: Literal["break_test"]
    # The change that causes the break.
    change_model: str
    change_column: str
    change_kind: str
    # The dbt test that breaks: its generic name (not_null/unique/relationships), unique_id
    # and the schema-file path to fix. ``via_reference`` marks a relationships test broken
    # through its *referenced* (parent) side rather than its own target column.
    test_name: str
    test_unique_id: str
    resource_path: Optional[str] = None
    via_reference: bool = False

    def code(self) -> str:
        """A short, stable diagnostic code for compiler-style rendering."""
        return "BREAK-TEST"


class ModelDependency(BaseModel):
    model_name: str
    depends_on: Set[str]


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    name: str
    schema_name: str = Field(alias="schema")  # Handle base model shadow attribute `schema`
    database: str
    columns: Dict[str, Column] = Field(default_factory=dict)
    metadata: Optional[Dict[str, Any]] = None
    unique_id: Optional[str] = None
    upstream: Set[str] = Field(default_factory=set)
    downstream: Set[str] = Field(default_factory=set)
    # Upstream columns this model uses only in predicates (WHERE / JOIN / HAVING / QUALIFY).
    predicate_sources: Set[str] = Field(default_factory=set)
    # Upstream column -> the predicate condition text it appears in.
    predicate_lineage: Dict[str, str] = Field(default_factory=dict)
    compiled_sql: Optional[str] = None
    language: Optional[str] = None
    resource_type: Literal["model", "source", "seed", "test", "exposure", "snapshot"]
    resource_path: Optional[str] = None
    source_identifier: Optional[str] = None
    source_name: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class SQLParseResult(BaseModel):
    column_lineage: Dict[str, List[ColumnLineage]]
    star_sources: Set[str] = Field(default_factory=set)
    # Upstream columns referenced only in predicates (WHERE / JOIN ON / HAVING / QUALIFY),
    # never projected. A change to one of these alters this model's row-set (and therefore
    # its aggregates), so it is a real — if indirect — downstream impact.
    predicate_sources: Set[str] = Field(default_factory=set)
    # Upstream column -> the predicate condition text it appears in (the "why" for the
    # row-set impact, e.g. ``status = 'flagged'``).
    predicate_lineage: Dict[str, str] = Field(default_factory=dict)
    # Columns whose upstream source parrant could not genuinely resolve — a phantom flatten
    # alias, a quoted pivot literal, a ``select * rename`` output, or a ``select *`` off an
    # unresolvable relation. The parser emits these INSTEAD of fabricating a source column, so
    # the set is complete and uncapped (display layers may cap; the machine surface must not).
    # ``model`` is left empty here (the parser has no model name); the registry stamps it.
    unresolved_edges: List[UnresolvedColumnEdge] = Field(default_factory=list)


class Coverage(BaseModel):
    models_in_manifest: int
    models_in_catalog: int
    parsed_ok: int
    parse_failed: int
    skipped_no_sql: int
    not_in_catalog_count: int
    failed_models: List[str] = Field(default_factory=list)
    skipped_models: List[str] = Field(default_factory=list)
    # Nodes whose compiled SQL the parser cannot read and that we deliberately do NOT
    # analyze at the column level (semantic views chief among them; more generally any
    # unparseable node). These are NOT a coverage failure — we chose not to parse them, so
    # they are excluded from the ``complete`` denominator and never counted as ``parse_failed``.
    # Model-level reach through them is still preserved from the manifest dependency graph.
    opaque: int = 0
    opaque_models: List[str] = Field(default_factory=list)
    complete: bool


class ImpactConfidence(BaseModel):
    reachable_models: int
    resolved_models: int
    # The coverage gap: reachable downstream models we could NOT analyze at the
    # column level, split by reason. This — not resolved-vs-reachable — is what makes
    # the impact a lower bound (many reachable models simply don't use the column).
    unanalyzable_models: int = 0
    # Reachable models with no column information available: absent from the catalog
    # AND with no parseable compiled SQL (e.g. a non-table relation such as a semantic
    # view, a python model, or a relation dbt has not built/compiled). A model that is
    # merely absent from the catalog but has parseable SQL is analyzable and NOT counted.
    no_column_info: int = 0
    parse_failed: int = 0
    # The actual unanalyzable model names, for UI/agent drill-down. In the machine
    # (JSON) surface these are the COMPLETE lists — a fail-closed consumer that
    # force-rebuilds "the models parrant couldn't analyze" must never miss one, so
    # len(no_column_info_models) == no_column_info and
    # len(parse_failed_models) == parse_failed always hold in machine output.
    # Display layers (markdown) cap the rendered names and set the *_truncated flags
    # below; the integer counts above remain the source of truth for totals.
    no_column_info_models: List[str] = Field(default_factory=list)
    parse_failed_models: List[str] = Field(default_factory=list)
    # Reachable models that DO expose columns but carry at least one unresolved-edge marker
    # (a phantom flatten alias, a quoted pivot literal, a ``select * rename`` output, or a
    # ``select *`` off an unresolvable relation — see :class:`UnresolvedColumnEdge`). Unlike
    # ``no_column_info`` / ``parse_failed`` these are analyzable at the column-LIST level, but
    # at least one of their column EDGES could not be genuinely resolved, so they cannot be
    # proven safe to skip. Their presence in a change's reachable set drops ``level`` to
    # ``partial`` (which widens the rebuild). COMPLETE, uncapped machine list — a fail-closed
    # consumer force-rebuilds every one, so len(partial_edges_models) == partial_edges always.
    partial_edges: int = 0
    partial_edges_models: List[str] = Field(default_factory=list)
    # Reachable nodes we deliberately do NOT analyze at the column level: their compiled SQL
    # is unparseable (semantic views chief among them; generally any node the parser cannot
    # read). Distinct from ``parse_failed`` (a node we *tried* and failed to derive lineage
    # for): opaque is a *choice*, not a failure — so these do not drag the coverage floor.
    # Model-level reach through them is still preserved from the manifest dependency graph, so
    # a change to an upstream still reaches (and rebuilds) them at model grain. Their presence
    # in a change's reachable set drops ``level`` to ``partial`` (fail-closed: we cannot see
    # column edges through them, so we widen the rebuild rather than prove anything skippable).
    # COMPLETE, uncapped machine list — len(opaque_models) == opaque always in machine output.
    opaque: int = 0
    opaque_models: List[str] = Field(default_factory=list)
    # Display-only truncation signals: False in machine output (lists are complete),
    # set True only by a display layer when it elided names from the rendered list.
    no_column_info_truncated: bool = False
    parse_failed_truncated: bool = False
    opaque_truncated: bool = False
    level: Literal["full", "partial"]


class Selection(BaseModel):
    """Policy-free minimal rebuild set, derived purely from the lineage diff.

    This is the policy-independent base the ``PolicyVerdict.build_set`` overlay sits on:
    it answers "which models must CI rebuild?" from the diff alone, with no authored policy.
    The rule is fail-closed —
    ``rebuild_models = {models with a non-equivalent reaching change}
    ∪ {reachable models parrant could not analyze} ∪ {the edited models themselves}`` —
    and ``skippable_models`` is the reachable complement (reached only by a provably
    additive/passthrough change at full confidence). When confidence is not full or any
    display list was truncated, ``skippable_models`` is emitted empty and ``rebuild_models``
    widens to every reachable model (``widened_to_all_reachable``): the honest "we could
    not prove anything safe to skip" state.
    """

    # Sentinel so a consumer never runs ``dbt build --select ""`` (which selects nothing
    # and exits green): branch on this, never on the selector string's emptiness.
    has_rebuild: bool = False
    # Sorted, deduplicated dbt node names that must be rebuilt.
    rebuild_models: List[str] = Field(default_factory=list)
    # Sorted reachable complement — reached only by an additive/passthrough change at full
    # confidence. Informational: the consumer decides whether to actually skip these.
    skippable_models: List[str] = Field(default_factory=list)
    # Space-joined ``rebuild_models`` — a drop-in for ``dbt build --select $(...)``.
    # Empty string exactly when ``has_rebuild`` is False.
    rebuild_selector: str = ""
    # Mirrors ``ImpactConfidence.level``.
    confidence_level: Literal["full", "partial"]
    # True when the widening branch fired: rebuild == all reachable, skippable == [].
    widened_to_all_reachable: bool = False


class ModelResolution(BaseModel):
    """Per-reachable-model column-resolution disposition (display/emission only).

    ``status`` retains, per model, the same partition the confidence pass already computes:
    ``catalog_backed``/``parsed`` are *resolved* (parrant has this model's columns);
    ``no_column_info``/``parse_failed``/``unresolved`` are *unanalyzable* (no columns to trace).
    A model with a resolved status has ``reason == None``.

    ``reason`` is a coarse, advisory hint at WHY an unanalyzable model could not be resolved —
    a prioritization signal for closing resolution gaps, not a load-bearing one. It NEVER
    upgrades a status and is NEVER consulted by the rebuild decision; stripping it leaves every
    status and every rebuild/skippable membership unchanged.
    """

    status: Literal[
        "catalog_backed",
        "parsed",
        "no_column_info",
        "parse_failed",
        "unresolved",
        "partial_edges",
        "opaque",
    ]
    # One of: star_off_cte, star_modifier, missing_catalog, python_model, unsupported_sql, other.
    # For a ``partial_edges`` status it is the marker construct (phantom_alias, unexpandable_star,
    # fabricated_column, star_rename, pivot_output, other). For an ``opaque`` status it names WHY
    # we did not parse it (``semantic_view`` or the general ``unparseable_sql``).
    reason: Optional[str] = None


class ResolutionReasonCount(BaseModel):
    """One coarse resolution reason and how many unanalyzable models carry it."""

    reason: str
    count: int


class ResolutionSummary(BaseModel):
    """Aggregate roll-up of the per-model resolution statuses over the reachable set.

    The per-status counts reconcile exactly with the reachable partition:
    ``catalog_backed + parsed + no_column_info + parse_failed + unresolved == reachable``,
    where ``unresolved`` now aggregates BOTH python models (no columns) AND ``partial_edges``
    models (columns present but at least one phantom/unresolvable source edge). ``partial_edges``
    below is the informational sub-count of the latter — it is a breakdown of ``unresolved``,
    NOT an extra partition, so it must not be added again into the reconciliation total.
    """

    reachable: int = 0
    catalog_backed: int = 0
    parsed: int = 0
    no_column_info: int = 0
    parse_failed: int = 0
    # Aggregate of models parrant could not resolve: python models with no columns PLUS
    # ``partial_edges`` models (columns present, but a source edge could not be resolved).
    unresolved: int = 0
    # Sub-count of ``unresolved``: reachable models with columns but >=1 unresolved-edge marker.
    partial_edges: int = 0
    # Reachable nodes we deliberately do NOT column-analyze (unparseable SQL, e.g. semantic
    # views). A distinct partition, so the reconciliation identity becomes
    # ``catalog_backed + parsed + no_column_info + parse_failed + unresolved + opaque == reachable``.
    opaque: int = 0
    # |rebuild_models ∩ {models whose status is not catalog_backed/parsed}|: how many rebuilds
    # are forced because parrant could not resolve the model, rather than by a proven reaching
    # change. Consumers can log this as a fraction of |rebuild_models| to measure how much
    # rebuild volume is driven by unresolved models rather than proven changes.
    rebuild_forced_by_nonresolution: int = 0
    # Coarse reasons ranked by frequency (advisory): the ranked resolution-gap backlog.
    top_reasons: List[ResolutionReasonCount] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Metabase cross-boundary lineage artifact (metabase_lineage.json).
#
# Snapshot of "which Metabase card reads which warehouse column, and which
# dashboards show that card", plus provenance. Produced by the credentialed
# ``metabase-extract`` step and consumed OFFLINE (zero credentials) by the gate,
# exactly like ``manifest.json`` / ``catalog.json``.
# ---------------------------------------------------------------------------


class MetabaseProvenance(BaseModel):
    """Where/when a snapshot came from — stamps snapshot age and the parse dialect.

    Credentials are NEVER stored here: only the (non-secret) base URL is stamped.
    """

    generated_at: str  # ISO-8601 UTC — stamps snapshot age
    metabase_base_url: str
    metabase_version: Optional[str] = None
    database_ids: List[int] = Field(default_factory=list)
    extractor_version: str
    dbt_adapter: Optional[str] = None  # dialect the native resolver parsed with


class MetabaseCoverage(BaseModel):
    """Resolution honesty for one snapshot — column-precise vs table-only vs unresolved.

    Same discipline as :class:`Coverage` / :class:`ImpactConfidence`: never guess,
    count what could not be resolved and expose a capped id sample.
    """

    cards_total: int
    cards_resolved_column: int  # column-precise (MBQL, or confident native)
    cards_resolved_table_only: int  # degraded to table grain (select *, complex SQL)
    cards_unresolved: int  # no warehouse relation resolved at all
    dashboards_total: int
    snippets_total: int
    unresolved_card_ids: List[int] = Field(default_factory=list)  # capped honesty sample
    table_only_card_ids: List[int] = Field(default_factory=list)


class MetabaseRelation(BaseModel):
    """A warehouse relation (``database.schema.table``) a card reads from.

    The join anchor: this normalized key maps to a dbt model via the manifest
    ``relation_name`` in the (later) reach phase. Normalized = lowercased, unquoted.
    """

    database: str
    schema_name: str = Field(alias="schema")
    table: str
    model_config = ConfigDict(populate_by_name=True)

    def key(self) -> str:
        """Normalized ``database.schema.table`` (lowercased, unquoted) — the dict key."""
        return f"{self.database}.{self.schema_name}.{self.table}".lower()


class MetabaseColumnRef(BaseModel):
    """One column a card reads, resolved to a warehouse relation + column.

    ``role`` records HOW the column is used (breakout/filter/field/...) so the policy
    engine can later filter on mechanism and the explorer can show "used as a filter".
    """

    relation: str  # normalized relation key (FK into ``MetabaseLineage.relations``)
    column: str  # lowercased warehouse column
    role: Literal["field", "breakout", "aggregation", "filter", "join", "order", "native"]
    confidence: Literal["high", "medium"] = "high"


class MetabaseCard(BaseModel):
    """A Metabase question and the warehouse columns/relations it reads.

    ``precision`` is first-class: ``column`` cards give column-precise reach; ``table``
    cards degrade to "the changed column is in a table this card reads" (still a valid
    dashboard-reach signal); ``none`` means no relation resolved at all.

    ``updated_at`` is the snapshot-time last-modified stamp (from ``/api/card``
    ``updated_at``), used for incremental reuse; it is ``None`` in v1 snapshots.
    """

    card_id: int
    name: str
    query_kind: Literal["mbql", "native"]
    precision: Literal["column", "table", "none"]
    collection_id: Optional[int] = None
    archived: bool = False
    database_id: Optional[int] = None
    columns: List[MetabaseColumnRef] = Field(default_factory=list)
    table_relations: List[str] = Field(default_factory=list)  # relation keys, table grain
    upstream_card_ids: List[int] = Field(default_factory=list)  # {{#id}} / card__<id> deps
    snippet_ids: List[int] = Field(default_factory=list)
    unresolved_reason: Optional[str] = None  # select_star | parse_failed | unknown_table | ...
    # snapshot-time last-modified stamp (ISO-8601 UTC), used for incremental reuse
    updated_at: Optional[str] = None
    # Asset metadata — a deep link to the card and human/ownership context, so a reached card
    # in impact/explorer output is clickable and attributable. All best-effort (None if absent).
    url: Optional[str] = None  # {base}/question/{card_id}
    description: Optional[str] = None
    collection_name: Optional[str] = None
    creator: Optional[str] = None  # email (else display name) of the card's creator
    last_edited_by: Optional[str] = None  # email/name from Metabase's last-edit-info


class MetabaseDashboard(BaseModel):
    """A Metabase dashboard and the cards it shows.

    ``meta`` (tier/owner/...) is a CONSUMER-CONFIGURABLE input to the extract — the tool
    never hardcodes an org's taxonomy. Absent meta is ``{}``; the policy
    engine treats that per its ``MissingMetaPolicy`` (fail-closed by default).

    ``updated_at`` is the snapshot-time last-modified stamp (from the ``/api/dashboard``
    list shell), used for incremental reuse; it is ``None`` in v1 snapshots.
    """

    dashboard_id: int
    name: str
    collection_id: Optional[int] = None
    url: Optional[str] = None
    card_ids: List[int] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)  # tier/owner/... for policy reach.where
    # snapshot-time last-modified stamp (ISO-8601 UTC), used for incremental reuse
    updated_at: Optional[str] = None
    # Asset metadata (best-effort; None if absent). ``url`` is already populated above.
    description: Optional[str] = None
    collection_name: Optional[str] = None
    creator: Optional[str] = None  # email (else display name) of the dashboard's creator
    last_edited_by: Optional[str] = None


class MetabaseLineage(BaseModel):
    """The ``metabase_lineage.json`` artifact — a self-contained snapshot.

    ``relations`` de-duplicates relation metadata; cards/dashboards reference relations
    and cards by key/id, mirroring the manifest's node/edge normalization (diff-friendly).

    ``schema_version`` 2 adds incremental support: cards and dashboards carry an
    ``updated_at`` last-modified stamp so a later extract can reuse unchanged entities.
    v1 artifacts (no ``updated_at``) remain readable — the field defaults to ``None``.
    """

    schema_version: int = 2
    provenance: MetabaseProvenance
    coverage: MetabaseCoverage
    relations: Dict[str, MetabaseRelation] = Field(default_factory=dict)
    cards: List[MetabaseCard] = Field(default_factory=list)
    dashboards: List[MetabaseDashboard] = Field(default_factory=list)


# ===========================================================================
# POLICY ENGINE — metadata-agnostic rule engine types.
#
# This block is APPEND-ONLY and self-contained so a concurrent append elsewhere
# in this file merges trivially. It declares the rule model (predicate -> action)
# and the PolicyVerdict the engine emits. No metadata key is privileged here; the
# consumer authors the rules (policy.yml). See lineage/policy.py for the engine.
# ===========================================================================


# --- vocabulary enums ------------------------------------------------------


class MatchAxis(str, Enum):
    """The axes a predicate leaf can match on.

    ``INFERRED_META`` mirrors ``META`` but resolves a key's value by folding UPSTREAM lineage
    (a column's classification inherited from where its data comes from) rather than reading
    only the node's own declared meta — see ``inferred_meta`` in ``lineage/policy.py``.

    ``CONFIG`` mirrors ``META`` but resolves a DOTTED key against the model's resolved dbt
    ``node.config`` (``grants.select``, ``materialized``, ``tags``, ``enabled`` …) rather than
    user ``meta`` — see ``config`` in ``lineage/policy.py``. Model-grained only.
    """

    CHANGE = "change"
    META = "meta"
    INFERRED_META = "inferred_meta"
    CONFIG = "config"
    REACH = "reach"
    STRUCTURAL = "structural"


class Operator(str, Enum):
    """Operator vocabulary for ``meta`` and ``change`` string/list/numeric matching."""

    EXISTS = "exists"
    ABSENT = "absent"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    MATCHES = "matches"
    INTERSECTS = "intersects"
    SUBSET_OF = "subset_of"
    NOT_SUBSET_OF = "not_subset_of"
    SUPERSET_OF = "superset_of"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"


class ReachKind(str, Enum):
    """What kind of downstream object a ``reach`` condition scans."""

    MODEL = "model"
    COLUMN = "column"
    EXPOSURE = "exposure"


class Mechanism(str, Enum):
    """Reach *mechanism* taxonomy — the machine-readable twin of ``_MECHANISM_LABELS``.

    Predicates match reach on these labels (recompute vs row-set vs pass-through).
    """

    DERIVED_RECOMPUTE = "derived_recompute"
    ROWSET_FILTER = "rowset_filter"
    RENAMED_PASSTHROUGH = "renamed_passthrough"
    DIRECT_PASSTHROUGH = "direct_passthrough"


class ActionKind(str, Enum):
    """What a fired rule contributes to the ``PolicyVerdict``."""

    BLOCK = "block"
    WARN = "warn"
    ADD_TO_BUILD_SET = "add-to-build-set"
    ADD_TO_TEST_SET = "add-to-test-set"
    NOTIFY = "notify"


class MissingMetaPolicy(str, Enum):
    """How an undecidable leaf (missing meta / type error / unresolved reach) resolves."""

    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"
    SKIP = "skip"


class GateDecision(str, Enum):
    """The gate ruling, combined most-severe-wins across all fired rules."""

    BLOCK = "block"
    WARN = "warn"
    ALLOW = "allow"

    @property
    def severity(self) -> int:
        """block=2, warn=1, allow=0 — for most-severe-wins combination."""
        return {GateDecision.BLOCK: 2, GateDecision.WARN: 1, GateDecision.ALLOW: 0}[self]


# --- leaf conditions -------------------------------------------------------


class ChangeCondition(BaseModel):
    """A fact about the subject ``ColumnChange`` itself."""

    field: Literal["kind", "semantic", "breaking", "model", "column"]
    op: Operator
    value: Optional[Any] = None


class MetaCondition(BaseModel):
    """A ``meta.<dotted.key>`` on the subject node/column (or a reached object)."""

    key: str
    op: Operator
    value: Optional[Any] = None


class ReachCondition(BaseModel):
    """A quantified condition over the subject's downstream reach.

    Satisfied when at least ``min_count`` reached objects (of ``kind``, optionally
    filtered to ``mechanism``) match the inner ``where`` predicate (which matches on
    each reached object's own ``meta.*``).
    """

    kind: ReachKind
    mechanism: Optional[List[Mechanism]] = None
    where: "Predicate"
    # ge=1: min_count 0 is a vacuously-true reach (matches with zero reached objects), which is
    # never a meaningful gate — reject it at config load rather than silently always-firing.
    min_count: int = Field(default=1, ge=1)


class StructuralCondition(BaseModel):
    """A boolean fact the pipeline already computes."""

    fact: Literal["provable_test_break", "touches_exposure", "reaches_anything"]


# --- predicate tree (recursive) --------------------------------------------


class Predicate(BaseModel):
    """Exactly one field is set: a boolean combinator OR a leaf condition.

    Combinators: ``all_`` (AND, alias ``all``), ``any_`` (OR, alias ``any``),
    ``not_`` (negation, alias ``not``). Leaves: ``change`` / ``meta`` / ``inferred_meta`` /
    ``config`` / ``reach`` / ``structural``. The one-of invariant is enforced by a model validator.
    """

    all_: Optional[List["Predicate"]] = Field(default=None, alias="all")
    any_: Optional[List["Predicate"]] = Field(default=None, alias="any")
    not_: Optional["Predicate"] = Field(default=None, alias="not")
    change: Optional[ChangeCondition] = None
    meta: Optional[MetaCondition] = None
    # ``inferred_meta`` shares ``MetaCondition``'s shape (key/op/value) but resolves the key's
    # value by folding UPSTREAM lineage rather than reading only the node's own declared meta —
    # see ``inferred_meta`` in ``lineage/policy.py``. An unresolvable value is UNKNOWN (fail-safe).
    inferred_meta: Optional[MetaCondition] = None
    # ``config`` shares ``MetaCondition``'s shape (key/op/value) but resolves the DOTTED key
    # against the subject model's resolved dbt ``node.config`` (``grants.select``,
    # ``materialized``, ``tags`` …) rather than user ``meta``. Model-grained. A missing dotted
    # path resolves to the EMPTY SET for set operators (present, not unknown) and to
    # UNKNOWN_MISSING for scalar operators — see ``config`` in ``lineage/policy.py``.
    config: Optional[MetaCondition] = None
    reach: Optional[ReachCondition] = None
    structural: Optional[StructuralCondition] = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _exactly_one(self) -> "Predicate":
        set_fields = [
            name
            for name in (
                "all_",
                "any_",
                "not_",
                "change",
                "meta",
                "inferred_meta",
                "config",
                "reach",
                "structural",
            )
            if getattr(self, name) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                "a predicate must set exactly one of "
                "all/any/not/change/meta/inferred_meta/config/reach/structural, got: "
                f"{sorted(set_fields) or 'none'}"
            )
        return self


# --- actions ---------------------------------------------------------------


class Action(BaseModel):
    """One effect a fired rule contributes to the ``PolicyVerdict``."""

    type: ActionKind
    include: Literal["reached", "subject", "both"] = "reached"
    mechanism: Optional[List[Mechanism]] = None
    channel: Optional[str] = None
    target: Optional[str] = None
    message: Optional[str] = None


# --- rule + policy ---------------------------------------------------------


class Rule(BaseModel):
    """A single ``predicate -> actions`` rule."""

    id: str
    description: Optional[str] = None
    scope: Literal["change", "aggregate"] = "change"
    predicate: Predicate
    action: List[Action]
    # Two independent fail-safe knobs: on_missing_meta governs an undecidable leaf
    # caused by a missing meta key / unresolved reach; on_error governs an operator/type
    # mismatch (a genuine evaluation error). Each falls back to the matching PolicyDefaults knob.
    on_missing_meta: Optional[MissingMetaPolicy] = None
    on_error: Optional[MissingMetaPolicy] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_action_list(cls, data: Any) -> Any:
        """A single ``action`` mapping is coerced to a length-1 list for authoring ease."""
        if isinstance(data, dict) and isinstance(data.get("action"), dict):
            data = {**data, "action": [data["action"]]}
        return data


class PolicyDefaults(BaseModel):
    """Policy-wide fail-safe defaults, overridable per rule.

    ``on_meaning_changed`` / ``on_indeterminate`` are the built-in semantic-severity knobs:
    they map a column's semantic classification straight to a gate contribution without any
    hand-written rule, and — being two separate knobs — let a proven meaning shift
    (``MEANING_CHANGED``) and an unprovable one (``INDETERMINATE``) be gated at *different*
    severities (e.g. block one, warn the other). Left unset, neither contributes anything, so
    a policy behaves exactly as before. They fold into the same most-severe-wins combination as
    every rule, so a user rule can only ever raise the decision, never lower one of these.
    """

    on_missing_meta: MissingMetaPolicy = MissingMetaPolicy.FAIL_CLOSED
    on_error: MissingMetaPolicy = MissingMetaPolicy.FAIL_CLOSED
    on_meaning_changed: Optional[GateDecision] = None
    on_indeterminate: Optional[GateDecision] = None


class Policy(BaseModel):
    """A parsed ``policy.yml`` — schema version + defaults + rules."""

    version: int
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    rules: List[Rule] = Field(default_factory=list)


# --- outputs ---------------------------------------------------------------


class Notification(BaseModel):
    """A notify intent the engine emits; routing is the consumer's CI job."""

    channel: str
    target: str
    message: str


class RuleHit(BaseModel):
    """One firing of a rule against a subject (or once, for aggregate scope)."""

    rule_id: str
    decision: GateDecision
    change_model: Optional[str] = None
    change_column: Optional[str] = None
    matched_reach: List[str] = Field(default_factory=list)
    actions: List[ActionKind] = Field(default_factory=list)
    # override cap (backward-compatible): when an override pragma capped this hit,
    # ``decision`` holds the effective/capped value (so ``_combine_decision`` needs no change)
    # while ``original_decision`` keeps the pre-cap value so the report can show the delta.
    overridden: bool = False
    original_decision: Optional[GateDecision] = None
    override_reason: Optional[str] = None
    # the backtest trust signal (backward-compatible, default False keeps serialization additive):
    # True when this hit fired via a *fail-safe UNKNOWN* resolution (a blocking rule under
    # ``fail_closed`` firing on an undecidable predicate) rather than a proven TRUE match. This is
    # the headline "rage-block footgun" column in ``policy test`` — a rule that blocks mostly via
    # fail-safe defaults, not real matches. NEVER set on builtin semantic-default hits (always
    # proven) nor perturbed by override caps. ``unknown_cause`` splits missing-vs-error for the
    # drill-down; the bool is the primary signal.
    fired_on_unknown: bool = False
    unknown_cause: Optional[Literal["missing", "error"]] = None


class UnprovenPolicyHit(BaseModel):
    """A suppressed-unknown firing: a NON-BLOCKING rule whose predicate stayed UNKNOWN under a
    ``fail_closed`` knob (issue #124).

    Under fail_closed only a *blocking* rule fires on UNKNOWN; a warn rule is suppressed so the
    engine never manufactures a spurious warning. But silence would make an all-warn pilot policy
    systematically understate what block mode will do — so each suppression is recorded here:
    the rule, the subject change, the UNKNOWN cause (missing meta vs evaluation error), and the
    leaf condition(s) that resolved UNKNOWN. Report-only telemetry: it contributes nothing to
    the gate decision, hits, or exit code.
    """

    rule_id: str
    change_model: Optional[str] = None
    change_column: Optional[str] = None
    unknown_cause: Literal["missing", "error"]
    # The leaf conditions that resolved UNKNOWN while this rule was evaluated, as compact
    # ``axis.key`` labels (e.g. ``meta.pii``, ``reach.exposure``) — the "what to tag" pointer.
    unknown_leaves: List[str] = Field(default_factory=list)


class PolicyVerdict(BaseModel):
    """The engine's output: a gate decision + accumulated build/test sets + notifications."""

    decision: GateDecision
    hits: List[RuleHit] = Field(default_factory=list)
    build_set: List[str] = Field(default_factory=list)
    test_set: List[str] = Field(default_factory=list)
    notifications: List[Notification] = Field(default_factory=list)
    evaluated_rules: int = 0
    fired_rules: int = 0
    # Honesty counters for fail-safe explainability (additive; see policy.py §7).
    unresolved_reach_count: int = 0
    skipped_missing_meta: int = 0
    # Suppressed-unknown telemetry (additive; issue #124): warn-rule conditions that a
    # fail_closed knob WOULD have fired were the rule blocking. ``unproven_count`` mirrors
    # ``len(unproven)`` for one-glance consumers of the JSON verdict.
    unproven: List[UnprovenPolicyHit] = Field(default_factory=list)
    unproven_count: int = 0

    def blocks(self) -> bool:
        """True when the gate decision is ``BLOCK``."""
        return self.decision is GateDecision.BLOCK


# Resolve the mutual forward references (Predicate <-> ReachCondition).
ReachCondition.model_rebuild()
Predicate.model_rebuild()


# ===========================================================================
# METABASE CROSS-BOUNDARY REACH — offline join honesty.
#
# Append-only block. When the Metabase artifact is joined into the impact reach
# (``--metabase``), the report carries this honesty signal so a fail-closed block
# driven by a STALE or coarse snapshot reads as such — never fabricated dashboard
# reach. Mirrors the discipline of ``Coverage`` / ``ImpactConfidence``.
# ===========================================================================


class MetabaseReachConfidence(BaseModel):
    """How trustworthy the appended Metabase dashboard reach is for one impact run.

    Two independent axes: snapshot *staleness* (age vs a threshold) and
    *resolution* (how many reached dashboards are backed by column-precise vs merely
    table-grain cards). An absent or stale snapshot degrades to "dbt-only reach + a
    warning"; it never fabricates dashboard reach.
    """

    snapshot_generated_at: Optional[str] = None
    snapshot_age_hours: Optional[float] = None
    stale: bool = False  # age > threshold OR artifact missing
    dashboards_reached: int = 0
    cards_column_precise: int = 0  # reached via a column-precise card
    cards_table_only: int = 0  # reached only via a table-grain card
    level: Literal["full", "partial", "absent"] = "absent"


# ===========================================================================
# POLICY BACKTEST REPORT — the `policy test` trust instrument.
#
# Append-only block. ``policy test`` replays a candidate policy over a git range
# (or a corpus of saved changesets) and reports, per rule, what the gate WOULD
# have ruled: would-BLOCK / would-WARN PRs, total firings, and — the headline
# trust column — how many firings were driven by a fail-safe UNKNOWN rather than a
# proven match (the "rage-block footgun"). Typed so ``--format json`` feeds
# a CI artifact / an agent over MCP with a stable schema and mypy stays honest.
# ===========================================================================


class BacktestFiredHit(BaseModel):
    """One rule firing within a single replayed point — the typed per-PR drill-down row.

    A small typed sub-model (rather than a loose dict) so the agent-facing JSON surface stays
    stable and mypy keeps covering it: ``rule_id`` + the (capped) ``decision`` + whether the
    firing came from a fail-safe UNKNOWN resolution.
    """

    rule_id: str
    decision: GateDecision
    fired_on_unknown: bool = False


class BacktestRuleStat(BaseModel):
    """Per-rule aggregate across the whole replayed range — the row a governance buyer reads.

    ``matched_zero`` (dead rule) is TRUE iff the rule never fired at all across the range
    (``fired_total == 0``). A rule that fired ONLY via fail-safe UNKNOWN is NOT ``matched_zero``
    (it has firings) but has ``fired_on_unknown == fired_total`` — the renderer distinguishes the
    two so a pure-fail-safe rule is never misread as healthy.
    """

    rule_id: str
    would_block_prs: int = 0
    would_warn_prs: int = 0
    fired_total: int = 0
    fired_on_unknown: int = 0
    matched_zero: bool = False


class BacktestPointResult(BaseModel):
    """The outcome of replaying the policy against one point (a commit or a fixture changeset)."""

    ref: str  # commit sha (git modes) or fixture filename (--changesets)
    source: str  # human label for the changeset source (e.g. "git-diff (<sha>^..<sha>)")
    total_changes: int
    unmapped_changes: int = 0  # .sql paths that mapped to no model in the HEAD registry
    parse_failures: List[str] = Field(default_factory=list)
    decision: str  # allow / warn / block
    blast_radius: int = 0
    fired: List[BacktestFiredHit] = Field(default_factory=list)
    sample_reach: List[str] = Field(default_factory=list)
    # --- selection measurement (pure observation of the per-point Selection block) ---
    # ``None`` for points replayed without a selection (a failed replay, or a service that
    # produced no selection block); such points are excluded from the aggregate stats.
    selection_widened: Optional[bool] = None  # Selection.widened_to_all_reachable
    skippable_models_count: Optional[int] = None  # len(Selection.skippable_models)
    # ResolutionSummary.rebuild_forced_by_nonresolution for this point (rebuilds forced
    # because parrant could not resolve the model, not by a proven reaching change).
    rebuild_forced_by_nonresolution: Optional[int] = None
    # ResolutionSummary.top_reasons flattened to {reason -> count} for cross-point aggregation.
    resolution_reasons: Dict[str, int] = Field(default_factory=dict)


class BacktestSelectionStats(BaseModel):
    """Aggregate selection widen-rate statistics across the replayed range (measurement only).

    Answers "how often does the rebuild selection give up and widen to every reachable model,
    and why?" over the whole backtest — pure aggregation of the per-point ``Selection`` /
    ``ResolutionSummary`` facts; it changes no selection or gating behavior.
    """

    # Points that carried a selection block (the denominator of every rate below).
    points_with_selection: int = 0
    # Points where ``widened_to_all_reachable`` was true — the safe over-build fired.
    widened_points: int = 0
    # ``widened_points / points_with_selection`` as a percentage (1 decimal; 0.0 when empty).
    widen_rate_pct: float = 0.0
    # Median ``len(skippable_models)`` across points with a selection (0.0 when empty).
    median_skippable_models: float = 0.0
    # Sum of per-point ``rebuild_forced_by_nonresolution`` — total rebuilds forced because a
    # model could not be resolved rather than by a proven reaching change.
    rebuild_forced_by_nonresolution_total: int = 0
    # Cross-point roll-up of the per-point resolution reasons, ranked by frequency: the
    # widening/forcing-reason backlog (which resolution gaps drive the widen rate).
    top_reasons: List[ResolutionReasonCount] = Field(default_factory=list)


class BacktestReport(BaseModel):
    """The assembled backtest: totals + per-rule aggregate + per-point drill-down + fidelity note.

    ``fidelity_note`` is mode-branched and MUST stay honest: in every mode the backtest ships without a
    per-commit base manifest, so the provable-break and semantic meaning-change BLOCK tiers are
    NOT exercised. The note states that plainly and never names an unavailable flag.
    """

    mode: Literal["git-diff", "changesets"]
    policy_source: str
    base: Optional[str] = None
    head: Optional[str] = None
    prs_replayed: int = 0
    prs_would_block: int = 0
    prs_would_warn: int = 0
    prs_skipped: int = 0  # points whose impact could not be computed (parse failures / errors)
    avg_blast_radius: float = 0.0
    rule_stats: List[BacktestRuleStat] = Field(default_factory=list)
    points: List[BacktestPointResult] = Field(default_factory=list)
    fidelity_note: str = ""
    warnings: List[str] = Field(default_factory=list)
    baseline_delta: Optional[Dict[str, Any]] = None
    # Aggregate selection widen-rate statistics (measurement only). ``None`` when no replayed
    # point carried a selection block, so an absent surface is never mistaken for a zero rate.
    selection_stats: Optional[BacktestSelectionStats] = None


# ===========================================================================
# POLICY INIT SCAN — the `policy init` manifest introspection.
#
# Append-only block. ``policy init`` reads the manifest + catalog once and
# computes which tool-owned signals actually exist (column-targeted tests,
# exposures) and a meta-key coverage histogram — the manifest scan. These types are
# internal to the scaffold flow (NOT part of any ``--format json`` surface), but
# they are typed rather than loose dicts per the house rule so mypy stays honest,
# mirroring the ``BacktestReport`` precedent. The emitter (lineage/policy_init.py)
# consumes a ``PolicyInitScan`` to decide which rules to enable vs comment and to
# print REAL coverage numbers in the inline notes.
# ===========================================================================


class MetaKeyCoverage(BaseModel):
    """One row of the meta-key coverage histogram: how many models/columns carry a key.

    ``key`` is the dotted meta path (``critical`` or ``governance.tier``) so it matches the
    engine's ``_dotted_get`` lookup verbatim. ``n_present`` is the count carrying it;
    ``total`` is the denominator (``total_models`` for the model histogram, ``total_columns``
    for the column histogram). ``pct`` is the rendered percentage used verbatim in the emitted
    coverage comment (e.g. "present on 95/1163 models (8%)").
    """

    key: str
    n_present: int
    total: int

    @property
    def pct(self) -> int:
        """Whole-percent coverage, guarded so a zero denominator yields 0 (never divides)."""
        if self.total == 0:
            return 0
        return round(100 * self.n_present / self.total)


class PolicyInitScan(BaseModel):
    """The read-only manifest/catalog scan that drives ``policy init`` (the manifest scan).

    Everything here is derived from the registry accessors — never guessed. The two
    ``*_present`` properties gate whether the corresponding tool-owned rule is emitted ENABLED
    (a signal that actually fires) versus commented-out with an honest "not detected" note.
    """

    total_models: int = 0
    total_columns: int = 0
    # Sum of ``get_column_tests`` over every (model, column) pair — column-targeted generic tests.
    column_test_count: int = 0
    models_with_column_tests: int = 0
    exposure_count: int = 0
    # Number of models declaring a non-empty ``config.grants.select`` — gates the commented
    # config-axis (PII over-grant) template, so it is only offered when dbt grants actually exist.
    models_with_grants: int = 0
    # Sorted by n_present desc, then key (stable, most-covered-first).
    model_meta_keys: List[MetaKeyCoverage] = Field(default_factory=list)
    column_meta_keys: List[MetaKeyCoverage] = Field(default_factory=list)

    @property
    def tests_present(self) -> bool:
        """True when the scan found at least one column-targeted generic test."""
        return self.column_test_count > 0

    @property
    def exposures_present(self) -> bool:
        """True when the scan found at least one exposure."""
        return self.exposure_count > 0

    @property
    def grants_present(self) -> bool:
        """True when at least one model declares a ``config.grants.select`` — the signal that
        the config-axis (PII over-grant) template is worth offering."""
        return self.models_with_grants > 0
