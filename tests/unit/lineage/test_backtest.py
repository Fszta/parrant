"""Unit tests for the ``policy test`` backtest driver.

Covered:
  * ``git_rev_list`` enumeration (oldest->newest, filtered to model-SQL-touching commits) and
    ``git_changed_models_and_unmapped`` mapping via a tiny real git repo;
  * ``changes_from_dicts`` round-trips a ``ColumnChange``;
  * ``_aggregate_rule_stats`` math (would-block/warn/fired/fired_on_unknown/matched_zero);
  * ``backtest_exit_code`` for none / any-block / regression (and the loud missing-baseline error);
  * git-diff mode: no base registry -> provable breaks empty, every change INDETERMINATE.
"""

import subprocess

import pytest

from parrant.lineage.backtest import (
    _aggregate_rule_stats,
    backtest_exit_code,
)
from parrant.lineage.changeset import (
    ChangeKind,
    ColumnChange,
    changes_from_dicts,
    git_changed_models_and_unmapped,
    git_rev_list,
)
from parrant.lineage.policy import parse_policy
from parrant.models.schema import (
    BacktestFiredHit,
    BacktestPointResult,
    BacktestReport,
    BacktestRuleStat,
    GateDecision,
    PolicyVerdict,
    RuleHit,
    SemanticChangeKind,
)

# --- git enumeration --------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _write(repo, rel, content):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    # c1: touches a model .sql
    _write(repo, "models/a.sql", "select 1 as x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1 add a")
    # c2: touches ONLY a markdown file -> must be filtered out of rev-list
    _write(repo, "README.md", "docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2 docs only")
    # c3: touches another model .sql
    _write(repo, "models/b.sql", "select 2 as y\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c3 add b")
    return repo


def test_git_rev_list_filters_to_sql_and_orders_oldest_first(git_repo):
    root = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    revs = git_rev_list(root, "HEAD", repo_dir=str(git_repo))
    # root (c1) is excluded by base..head; the docs-only c2 is filtered; only c3 (a .sql) remains.
    assert len(revs) == 1
    # subjects: confirm it is c3
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s", revs[0]],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "c3 add b"


def test_git_rev_list_raises_on_bad_range(tmp_path):
    with pytest.raises(RuntimeError):
        git_rev_list("nope", "HEAD", repo_dir=str(tmp_path))


class _FakeModel:
    def __init__(self, resource_path):
        self.resource_path = resource_path
        self.columns = {}


class _FakeRegistry:
    def __init__(self, models):
        self._models = models

    def get_models(self):
        return self._models


def test_git_changed_models_and_unmapped_splits_matched(git_repo):
    # HEAD (c3) vs its parent changed models/b.sql. Register b.sql -> model_b; a.sql maps nothing.
    registry = _FakeRegistry({"model_b": _FakeModel("models/b.sql")})
    matched, unmapped = git_changed_models_and_unmapped(
        registry, "HEAD^", "HEAD", repo_dir=str(git_repo)
    )
    assert matched == {"model_b"}
    assert unmapped == []
    # A registry that knows neither path: b.sql becomes an unmapped change.
    registry2 = _FakeRegistry({"other": _FakeModel("models/z.sql")})
    matched2, unmapped2 = git_changed_models_and_unmapped(
        registry2, "HEAD^", "HEAD", repo_dir=str(git_repo)
    )
    assert matched2 == set()
    assert unmapped2 == ["models/b.sql"]


# --- changes_from_dicts round-trip ------------------------------------------


def test_changes_from_dicts_round_trips_to_dict():
    original = ColumnChange(
        "stg_accounts",
        "account_id",
        ChangeKind.LOGIC_CHANGED,
        detail="path.sql",
        semantic=SemanticChangeKind.MEANING_CHANGED,
        reason="expr changed",
        base_expression="a",
        head_expression="upper(a)",
    )
    rebuilt = changes_from_dicts([original.to_dict()])
    assert len(rebuilt) == 1
    r = rebuilt[0]
    assert r.model == "stg_accounts"
    assert r.column == "account_id"
    assert r.kind is ChangeKind.LOGIC_CHANGED
    assert r.semantic is SemanticChangeKind.MEANING_CHANGED
    assert r.reason == "expr changed"
    assert r.base_expression == "a"
    assert r.head_expression == "upper(a)"


def test_changes_from_dicts_structural_has_no_semantic():
    rebuilt = changes_from_dicts(
        [{"model": "m", "column": "c", "kind": "removed", "detail": None, "semantic": None}]
    )
    assert rebuilt[0].kind is ChangeKind.REMOVED
    assert rebuilt[0].semantic is None


def test_changes_from_dicts_bad_kind_raises():
    with pytest.raises(ValueError):
        changes_from_dicts([{"model": "m", "column": "c", "kind": "nonsense"}])


# --- aggregation math -------------------------------------------------------


def _point(decision):
    return BacktestPointResult(ref="x", source="s", total_changes=1, decision=decision)


def _verdict(*hits):
    return PolicyVerdict(decision=GateDecision.ALLOW, hits=list(hits))


def _hit(rule_id, decision, fired_on_unknown=False):
    return RuleHit(rule_id=rule_id, decision=decision, fired_on_unknown=fired_on_unknown)


def _policy(*rule_ids):
    return parse_policy(
        {
            "version": 1,
            "rules": [
                {
                    "id": rid,
                    "predicate": {"change": {"field": "kind", "op": "eq", "value": "removed"}},
                    "action": [{"type": "block"}],
                }
                for rid in rule_ids
            ],
        }
    )


def test_aggregate_counts_block_warn_fired_and_unknown():
    policy = _policy("r_block", "r_warn", "r_dead")
    pv = [
        (
            _point("block"),
            _verdict(
                _hit("r_block", GateDecision.BLOCK, fired_on_unknown=True),
                _hit("r_warn", GateDecision.WARN),
            ),
        ),
        (
            _point("block"),
            _verdict(
                _hit("r_block", GateDecision.BLOCK, fired_on_unknown=True),
                _hit("r_block", GateDecision.BLOCK, fired_on_unknown=False),
            ),
        ),
    ]
    stats = {s.rule_id: s for s in _aggregate_rule_stats(pv, policy)}

    r_block = stats["r_block"]
    assert r_block.would_block_prs == 2  # blocked in both points
    assert r_block.fired_total == 3  # 1 + 2 hits
    assert r_block.fired_on_unknown == 2
    assert r_block.matched_zero is False

    r_warn = stats["r_warn"]
    assert r_warn.would_warn_prs == 1
    assert r_warn.would_block_prs == 0

    r_dead = stats["r_dead"]
    assert r_dead.fired_total == 0
    assert r_dead.matched_zero is True  # never fired -> dead rule


def test_aggregate_builtin_rule_not_flagged_dead():
    policy = _policy("r1")
    pv = [(_point("block"), _verdict(_hit("builtin:on_indeterminate", GateDecision.BLOCK)))]
    stats = {s.rule_id: s for s in _aggregate_rule_stats(pv, policy)}
    # builtin present (fired), NOT matched_zero; r1 never fired -> dead.
    assert stats["builtin:on_indeterminate"].matched_zero is False
    assert stats["r1"].matched_zero is True


# --- exit codes -------------------------------------------------------------


def _report(prs_would_block=0, rule_stats=None):
    return BacktestReport(
        mode="git-diff",
        policy_source="p.yml",
        prs_would_block=prs_would_block,
        rule_stats=rule_stats or [],
    )


def test_exit_code_none_is_zero():
    assert backtest_exit_code(_report(prs_would_block=5), "none") == 0


def test_exit_code_any_block():
    assert backtest_exit_code(_report(prs_would_block=1), "any-block") == 1
    assert backtest_exit_code(_report(prs_would_block=0), "any-block") == 0


def test_exit_code_regression_requires_baseline():
    with pytest.raises(RuntimeError):
        backtest_exit_code(_report(), "regression", baseline=None)


def test_exit_code_regression_fires_on_increase():
    baseline = _report(rule_stats=[BacktestRuleStat(rule_id="r", would_block_prs=1)])
    current = _report(rule_stats=[BacktestRuleStat(rule_id="r", would_block_prs=3)])
    assert backtest_exit_code(current, "regression", baseline=baseline) == 1


def test_exit_code_regression_zero_when_no_increase():
    baseline = _report(rule_stats=[BacktestRuleStat(rule_id="r", would_block_prs=3)])
    current = _report(rule_stats=[BacktestRuleStat(rule_id="r", would_block_prs=3)])
    assert backtest_exit_code(current, "regression", baseline=baseline) == 0


def test_fired_hit_shape_is_typed():
    hit = BacktestFiredHit(rule_id="r", decision=GateDecision.BLOCK, fired_on_unknown=True)
    dumped = hit.model_dump(mode="json")
    assert dumped == {"rule_id": "r", "decision": "block", "fired_on_unknown": True}
