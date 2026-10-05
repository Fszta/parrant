"""CI integration for diff-driven impact: sticky PR comment + severity gate.

 answers "what does this change break?" as a report. puts that report where
the decision is made — the pull request — with two pieces:

- a *sticky* Markdown comment, found-or-updated by a hidden HTML marker so
  re-runs edit one comment in place instead of spamming the thread;
- a configurable severity gate that maps the aggregated impact summary to a
  process exit code, defaulting to *warn* (never block) so a team can adopt the
  check before it enforces anything.

Only the GitHub REST API is used (via ``requests``); the PR context is resolved
from the standard GitHub Actions environment so the shipped ``action.yml`` needs
no extra wiring.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Hidden HTML comment identifying our PR comment so re-runs update it in place.
COMMENT_MARKER = "<!-- parrant:impact -->"

_DEFAULT_API = "https://api.github.com"
_TIMEOUT = 30


class FailOn(str, Enum):
    """Severity gate policy — which impact makes the check fail (non-zero exit).

    Defaults to :attr:`NONE` (warn only): the proposal calls for earning trust
    with a non-blocking check before flipping the gate to blocking.
    """

    NONE = "none"  # never fail — comment only
    TESTS = "tests"  # fail only on a PROVABLE break (a dbt test the change orphans)
    EXPOSURES = "exposures"  # fail if a business-facing exposure is affected
    CRITICAL = "critical"  # fail if a downstream column recomputes derived logic
    ANY = "any"  # fail if the change touches anything downstream
    POLICY = "policy"  # fail when the metadata-agnostic policy engine returns a BLOCK verdict

    @property
    def blocks(self) -> bool:
        return self is not FailOn.NONE


def gate_exit_code(
    summary: dict[str, Any],
    fail_on: FailOn,
    policy_verdict: Any | None = None,
) -> int:
    """Map an aggregated-impact ``summary`` to an exit code under ``fail_on``.

    Returns ``1`` when the policy is tripped, ``0`` otherwise. An empty/missing
    summary is always a pass — no impact never blocks.

    ``policy_verdict`` is only consulted under :attr:`FailOn.POLICY`: the gate fails
    when a policy verdict is present and ``policy_verdict.blocks()`` is true. Every
    other branch ignores it, so passing a verdict never perturbs the legacy gates
    (backward compatible).
    """
    if fail_on == FailOn.POLICY:
        return 1 if policy_verdict is not None and policy_verdict.blocks() else 0
    if fail_on == FailOn.TESTS:
        return 1 if summary.get("provable_break_count", 0) > 0 else 0
    if fail_on == FailOn.EXPOSURES:
        return 1 if summary.get("affected_exposures", 0) > 0 else 0
    if fail_on == FailOn.CRITICAL:
        return 1 if summary.get("critical_count", 0) > 0 else 0
    if fail_on == FailOn.ANY:
        touched = (
            summary.get("affected_models", 0)
            or summary.get("affected_columns", 0)
            or summary.get("affected_exposures", 0)
        )
        return 1 if touched else 0
    return 0  # FailOn.NONE and any unknown policy: warn only.


def highest_tripped_level(summary: dict[str, Any]) -> str:
    """Return the most severe gate level this ``summary`` trips, ignoring policy.

    Walks the blocking policies from most to least severe and returns the first whose
    gate condition is met, so downstream workflow steps can branch on the real reach of
    a change (``tests`` > ``exposures`` > ``critical`` > ``any``) independently of the
    configured ``fail-on``. A provable break (``tests``) is the most severe. Returns
    ``"none"`` when nothing downstream is touched.
    """
    for level in (FailOn.TESTS, FailOn.EXPOSURES, FailOn.CRITICAL, FailOn.ANY):
        if gate_exit_code(summary, level):
            return level.value
    return FailOn.NONE.value


def write_github_outputs(report: dict[str, Any]) -> bool:
    """Emit machine-readable results to ``$GITHUB_OUTPUT`` for the composite action.

    Writes ``affected_models``, ``affected_columns``, ``affected_exposures`` and
    ``tripped_level`` so an adopting workflow can wire the Action's ``outputs:`` into
    downstream steps. A no-op (returns ``False``) when ``$GITHUB_OUTPUT`` is unset, so
    running the CLI outside GitHub Actions is unaffected.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return False

    summary = report.get("summary", {}) or {}
    values = {
        "affected_models": int(summary.get("affected_models", 0)),
        "affected_columns": int(summary.get("affected_columns", 0)),
        "affected_exposures": int(summary.get("affected_exposures", 0)),
        "provable_breaks": int(summary.get("provable_break_count", 0)),
        "verdict": str(report.get("verdict", "")),
        "tripped_level": highest_tripped_level(summary),
        # number of honored override pragmas (0 when none). Always emitted so the output
        # set stays stable-shaped for adopting workflows.
        "overrides_applied": len(report.get("overrides", []) or []),
    }
    # Additive policy-engine outputs — only when a policy actually ran (report carries
    # a policy_verdict). Absent policy leaves the legacy output set byte-for-byte intact.
    policy_verdict = report.get("policy_verdict")
    if isinstance(policy_verdict, dict):
        values["policy_decision"] = str(policy_verdict.get("decision", ""))
        values["build_set_size"] = len(policy_verdict.get("build_set", []) or [])
        values["test_set_size"] = len(policy_verdict.get("test_set", []) or [])
    try:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())
    except OSError as exc:
        logger.warning("Could not write GitHub Action outputs: %s", exc)
        return False
    return True


def write_selector_outputs(report: dict[str, Any]) -> bool:
    """Emit the policy-free rebuild selection to ``$GITHUB_OUTPUT`` for a selective build.

    Projects ``report["selection"]`` verbatim — it never recomputes — writing exactly two keys:
    ``has_rebuild`` (lowercased ``true``/``false``, so a shell ``[ "$x" = "true" ]`` works) and
    ``rebuild_selector`` (the space-joined dbt node-name selector, a single line, empty exactly
    when ``has_rebuild`` is false). Keys are distinct from :func:`write_github_outputs`, so both
    may run in one invocation without clobbering each other.

    A no-op returning ``False`` when ``$GITHUB_OUTPUT`` is unset (running outside GitHub Actions
    is unaffected) or when the report carries no selection block. Never raises and never affects
    the process exit code.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return False

    selection = report.get("selection")
    if not isinstance(selection, dict):
        return False

    values = {
        "has_rebuild": str(bool(selection.get("has_rebuild", False))).lower(),
        "rebuild_selector": str(selection.get("rebuild_selector", "")),
    }
    try:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())
    except OSError as exc:
        logger.warning("Could not write selector GitHub Action outputs: %s", exc)
        return False
    return True


def with_marker(body: str) -> str:
    """Prefix a comment body with the hidden sticky marker (idempotent)."""
    if COMMENT_MARKER in body:
        return body
    return f"{COMMENT_MARKER}\n{body}"


@dataclass(frozen=True)
class GitHubContext:
    """Everything needed to post/update a comment on one PR."""

    repo: str  # "owner/name"
    pr_number: int
    token: str
    api_url: str = _DEFAULT_API


def resolve_pr_number(explicit: int | None = None) -> int | None:
    """Resolve the PR number, preferring an explicit value over the GH event.

    In GitHub Actions the ``pull_request`` event payload carries the number at
    ``GITHUB_EVENT_PATH``; some events expose it as a top-level ``number``.
    """
    if explicit:
        return int(explicit)

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path) as handle:
                event = json.load(handle)
        except (OSError, ValueError):
            return None
        number = event.get("pull_request", {}).get("number")
        if number is None:
            number = event.get("number")
        if number is not None:
            try:
                return int(number)
            except (TypeError, ValueError):
                return None
    return None


def resolve_context(
    token: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
) -> GitHubContext | None:
    """Build a :class:`GitHubContext` from explicit args + the GH Actions env.

    Returns ``None`` when any of token / repo / PR number is missing, so callers
    can degrade gracefully (skip the comment) rather than crash outside CI.
    """
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    number = resolve_pr_number(pr_number)
    api_url = os.environ.get("GITHUB_API_URL", _DEFAULT_API).rstrip("/")

    if not (token and repo and number):
        return None
    return GitHubContext(repo=repo, pr_number=int(number), token=token, api_url=api_url)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_comment_id(
    session: Any, base_url: str, headers: dict[str, str], marker: str
) -> int | None:
    """Return the id of the existing marked comment, paging through all comments."""
    page = 1
    while True:
        resp = session.get(
            base_url, headers=headers, params={"per_page": 100, "page": page}, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        comments = resp.json()
        if not comments:
            return None
        for comment in comments:
            if marker in (comment.get("body") or ""):
                return comment.get("id")
        if len(comments) < 100:
            return None
        page += 1


def post_sticky_comment(
    ctx: GitHubContext,
    body: str,
    marker: str = COMMENT_MARKER,
    session: Any = None,
) -> str:
    """Find-or-update the sticky impact comment on the PR.

    Returns ``"updated"`` if an existing marked comment was edited, else
    ``"created"``. The marker is injected into ``body`` if not already present.
    """
    session = session or requests
    headers = _headers(ctx.token)
    marked_body = with_marker(body) if marker == COMMENT_MARKER else body
    issue_comments = f"{ctx.api_url}/repos/{ctx.repo}/issues/{ctx.pr_number}/comments"

    existing_id = _find_comment_id(session, issue_comments, headers, marker)
    if existing_id is not None:
        resp = session.patch(
            f"{ctx.api_url}/repos/{ctx.repo}/issues/comments/{existing_id}",
            headers=headers,
            json={"body": marked_body},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return "updated"

    resp = session.post(
        issue_comments, headers=headers, json={"body": marked_body}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return "created"
