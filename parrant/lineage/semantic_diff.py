"""Pure AST semantic-diff engine for per-column defining expressions.

No I/O, no dbt artifacts, no registry — SQL expression strings in, a verdict out. This is
the first factor of the roadmap formula (AST semantic-diff × column-level lineage). Its only
consumer is ``changeset.py``.

Canonicalization pipeline:
``parse_one(sql, dialect)`` → ``normalize_identifiers(dialect)`` →
render with ``.sql(dialect=..., comments=False, normalize_functions="upper")``.

Before that AST path, a lexical *comment-stripped token signature* proves the one equivalence
that has no semantic content — a comment- and/or whitespace-only edit. Tokenizing is far more
permissive than ``parse_one``, so this route stays correct on dialect-specific SQL that has no
full AST (semi-structured access, ``match_recognize``, …); without it a purely cosmetic edit to
such a model was misclassified ``INDETERMINATE``. It is sound because the tokenizer preserves
string / quoted-identifier *contents* (a ``--`` inside a string is data, never a comment).

Fail-safe is load-bearing: anything that is not a *proven* ``EQUIVALENT`` is classified as
breaking (``MEANING_CHANGED`` or ``INDETERMINATE``). A parse failure never yields "equal".

Soundness note — why there is no ``simplify`` pass: sqlglot's ``simplify`` applies boolean
absorption / complement / collapse rules (``a OR NOT a`` → ``TRUE``, ``x AND NOT x`` → ``FALSE``,
``(x AND y) OR (x AND NOT y)`` → ``x``) that are valid in two-valued logic but **wrong under
SQL's three-valued (NULL) logic** — the two sides differ whenever an operand is NULL. Running it
made the engine report such genuinely-breaking rewrites as ``EQUIVALENT`` (a false negative that
silently passes a breaking change — the one thing this engine must never do). Canonicalization is
therefore limited to parse + identifier/whitespace/comment/function-name normalization, which is
provably meaning-preserving. This is conservative: sound cosmetic folds that only ``simplify``
provided (redundant parens, commutative reordering) now read as ``MEANING_CHANGED`` — over-blocking,
the correct failure direction for a gate. Restoring them soundly (a curated, NULL-safe rewrite set)
is deferred follow-up work.
"""

from __future__ import annotations

from functools import lru_cache

from sqlglot import exp, parse_one, tokenize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from parrant.models.schema import SemanticChangeKind, SemanticDiff

# Sentinel prefix for a canonical key we could not parse. Namespaced so a parse-failure key
# can never accidentally equal a real canonicalized expression string.
_UNPARSEABLE_PREFIX: str = "\x00unparseable:"

# Field/record separators for the lexical token signature — control chars that cannot occur
# in a SQL token's type name or text, so the join is unambiguous.
_TOKEN_FIELD_SEP: str = "\x1e"
_TOKEN_RECORD_SEP: str = "\x1f"


@lru_cache(maxsize=4096)
def comment_free_token_signature(expr_sql: str, dialect: str | None = None) -> str | None:
    """A lexical, comment- and whitespace-insensitive signature of a SQL string.

    Tokenizes with sqlglot and joins each token's ``(type, text)``, deliberately DROPPING the
    per-token ``comments`` metadata. Two SQL strings that differ ONLY by comments and/or
    whitespace therefore produce the SAME signature; a change to any real token (identifier,
    literal, operator, keyword) changes it. Returns ``None`` when tokenizing raises.

    This is the sound, robust counterpart to the AST path for the one property that has no
    semantic content — comments and formatting:

    * **Sound.** The tokenizer keeps string / quoted-identifier *contents* intact (a ``--`` or
      ``/* */`` inside a string is a ``STRING`` token, never a comment), so this neutralizes
      comments and whitespace and *nothing* semantic — it can never fold a real value change.
    * **Robust.** Tokenizing is far more permissive than ``parse_one``: it succeeds on
      dialect-specific SQL that has no full AST under the configured dialect (semi-structured
      access, ``match_recognize``, …). That is exactly the case where the AST path returns
      ``None`` and a comment-only edit would otherwise fall to ``INDETERMINATE``.

    It is intentionally *stricter* than AST canonicalization (it is case-sensitive and does not
    reorder), so ``sig(a) == sig(b)`` proves comment/whitespace-only equivalence, and anything
    it does not prove still flows to the AST path, which folds identifier / function-name case.
    """
    try:
        tokens = tokenize(expr_sql, dialect=dialect)
    except Exception:
        return None
    return _TOKEN_RECORD_SEP.join(
        f"{token.token_type.name}{_TOKEN_FIELD_SEP}{token.text}" for token in tokens
    )


@lru_cache(maxsize=4096)
def canonicalize_expression(expr_sql: str, dialect: str | None = None) -> exp.Expression | None:
    """Parse a SQL expression fragment and return its canonical AST, or ``None``.

    Pipeline: ``parse_one(expr_sql, dialect)`` → ``normalize_identifiers(dialect)``.
    Canonicalization folds only *provably* meaning-preserving cosmetic differences
    (whitespace, comments, identifier case, function-name case) into one form so two
    cosmetically-different-but-equivalent expressions produce equal ASTs (``==``). It deliberately
    does **not** run sqlglot's ``simplify`` — see the module docstring's soundness note: those
    boolean rewrites are unsound under SQL's three-valued (NULL) logic and would let a breaking
    change be reported ``EQUIVALENT``.

    Returns ``None`` when ``parse_one`` raises (``ParseError`` / unsupported) — the caller must
    treat ``None`` as *indeterminate → breaking* (fail-safe), never as "equal".
    """
    try:
        expression = parse_one(expr_sql, dialect=dialect)
    except Exception:
        return None

    return normalize_identifiers(expression, dialect=dialect)


@lru_cache(maxsize=4096)
def canonical_key(expr_sql: str | None, dialect: str | None = None) -> str:
    """Return a stable, hashable canonical key for an expression, for signature comparison.

    - Parseable  → the canonical SQL string
      (``.sql(dialect=dialect, comments=False, normalize_functions="upper")``).
    - ``None`` / unparseable → a value in the disjoint ``_UNPARSEABLE_PREFIX`` namespace, so an
      unparseable fragment can never equal a canonical form. Within that namespace we key on the
      *comment-stripped token signature* when we can tokenize (so two fragments differing ONLY by
      comments / whitespace still share a key, suppressing the cosmetic edit), and fall back to
      the raw stripped string when even tokenizing fails (byte-identical fragments still match).

    This is the drop-in replacement for ``changeset._normalize_sql`` inside the per-column
    signature: equal keys ⇒ semantically equal ⇒ not a logic change.
    """
    raw = expr_sql or ""
    if expr_sql is None:
        return _UNPARSEABLE_PREFIX + raw.strip()

    canonical = canonicalize_expression(expr_sql, dialect)
    if canonical is None:
        token_signature = comment_free_token_signature(expr_sql, dialect)
        if token_signature is not None:
            return _UNPARSEABLE_PREFIX + token_signature
        return _UNPARSEABLE_PREFIX + raw.strip()

    return canonical.sql(dialect=dialect, comments=False, normalize_functions="upper")


def compare_expressions(
    base_sql: str | None, head_sql: str | None, dialect: str | None = None
) -> SemanticDiff:
    """Classify the change between two column-defining expressions.

    Decision order (fail-safe):
      1. both ``None``/empty, or byte-identical raw string → ``EQUIVALENT`` (equal=True).
      2. comment/whitespace-only difference (equal comment-stripped token signatures) →
         ``EQUIVALENT`` (equal=True). This runs BEFORE the parse step so a purely cosmetic
         edit is proven equivalent even when the SQL uses dialect-specific syntax that
         ``parse_one`` cannot build an AST for — the cause of comment-only edits being
         misclassified ``INDETERMINATE``.
      3. either side fails to parse (``canonicalize_expression`` → ``None``) → ``INDETERMINATE``.
      4. canonical ASTs equal (``==``) → ``EQUIVALENT`` (equal=True).
      5. otherwise → ``MEANING_CHANGED`` (equal=False).

    ``reason`` is a short display string (e.g. ``"expressions are semantically equivalent"``,
    ``"could not parse base expression"``, ``"expression meaning changed"``). It is advisory;
    ``kind`` / ``equal`` are the load-bearing outputs.
    """
    # 1. Fast paths: both empty, or byte-identical raw strings.
    if not base_sql and not head_sql:
        return SemanticDiff(
            equal=True,
            kind=SemanticChangeKind.EQUIVALENT,
            reason="both expressions are empty",
        )
    if base_sql == head_sql:
        return SemanticDiff(
            equal=True,
            kind=SemanticChangeKind.EQUIVALENT,
            reason="expressions are identical",
        )

    # 2. Fail-safe: an empty side against a non-empty side is not provably equivalent.
    if not base_sql or not head_sql:
        side = "base" if not base_sql else "head"
        return SemanticDiff(
            equal=False,
            kind=SemanticChangeKind.INDETERMINATE,
            reason=f"missing {side} expression",
        )

    # 3. Lexical proof (runs before parse): identical apart from comments / whitespace ⇒
    # provably EQUIVALENT. Sound (comments/whitespace carry no meaning; string contents are
    # preserved) and, unlike the AST path below, succeeds even when parse_one cannot build an
    # AST for dialect-specific SQL — the real cause of comment-only edits going INDETERMINATE.
    base_tokens = comment_free_token_signature(base_sql, dialect)
    if base_tokens is not None and base_tokens == comment_free_token_signature(head_sql, dialect):
        return SemanticDiff(
            equal=True,
            kind=SemanticChangeKind.EQUIVALENT,
            reason="expressions differ only by comments or whitespace",
        )

    base_canonical = canonicalize_expression(base_sql, dialect)
    if base_canonical is None:
        return SemanticDiff(
            equal=False,
            kind=SemanticChangeKind.INDETERMINATE,
            reason="could not parse base expression",
        )
    head_canonical = canonicalize_expression(head_sql, dialect)
    if head_canonical is None:
        return SemanticDiff(
            equal=False,
            kind=SemanticChangeKind.INDETERMINATE,
            reason="could not parse head expression",
        )

    # 3. Canonical ASTs equal ⇒ cosmetic-only change.
    if base_canonical == head_canonical:
        return SemanticDiff(
            equal=True,
            kind=SemanticChangeKind.EQUIVALENT,
            reason="expressions are semantically equivalent",
        )

    # 4. Both parse, canonical forms differ ⇒ a real meaning change.
    return SemanticDiff(
        equal=False,
        kind=SemanticChangeKind.MEANING_CHANGED,
        reason="expression meaning changed",
    )
