#!/usr/bin/env python3
"""Generate a conventional-commits changelog section for a release.

Deterministic companion to the Release workflow: the version is decided by the
workflow (via `poetry version <bump>`), NOT inferred from commit messages, so a
`BREAKING CHANGE` no longer silently forces a major bump. This script only turns
the commits since the previous tag into a formatted Markdown section.

Usage:
    gen_changelog.py <new_version> [<prev_tag>]

Prints the Markdown section to stdout. Env (optional, with sensible fallbacks):
    GITHUB_SERVER_URL   default https://github.com
    GITHUB_REPOSITORY   default Fszta/parrant
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Conventional-commit types surfaced in the changelog, in display order.
# Anything else (chore, docs, style, refactor, test, ci, build) is hidden —
# matching the conventionalcommits preset the project used previously.
SECTIONS = [("feat", "Features"), ("fix", "Bug Fixes"), ("perf", "Performance Improvements")]

COMMIT_RE = re.compile(r"^(?P<type>\w+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: gen_changelog.py <new_version> [<prev_tag>]", file=sys.stderr)
        return 2

    new_version = sys.argv[1].lstrip("v")
    prev_tag = sys.argv[2] if len(sys.argv) > 2 else ""
    # Defensively collapse a `git describe` long-form ref (vX.Y.Z-<n>-g<sha>)
    # back to the bare tag: a stray long form here silently narrows the log
    # range to (almost) nothing and produces an empty changelog section.
    prev_tag = re.sub(r"-\d+-g[0-9a-f]+$", "", prev_tag)

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "Fszta/parrant")
    repo_url = f"{server}/{repo}"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rng = f"{prev_tag}..HEAD" if prev_tag else "HEAD"
    log = git("log", rng, "--no-merges", "--pretty=%H%x09%s")

    buckets: dict = {key: [] for key, _ in SECTIONS}
    breaking: list = []
    for line in filter(None, log.splitlines()):
        full, subj = line.split("\t", 1)
        m = COMMIT_RE.match(subj)
        if not m:
            continue
        typ, scope, bang, desc = (
            m.group("type"),
            m.group("scope"),
            m.group("bang"),
            m.group("desc"),
        )
        # Release/CI plumbing is never user-facing: drop it even when a commit is
        # mis-typed as feat/fix (e.g. `fix(ci): ...`) instead of `ci: ...`.
        if scope in ("ci", "release"):
            continue
        link = f"([{full[:7]}]({repo_url}/commit/{full}))"
        scope_s = f"**{scope}:** " if scope else ""
        bullet = f"* {scope_s}{desc} {link}"
        if bang:
            breaking.append(bullet)
        if typ in buckets:
            buckets[typ].append(bullet)

    if prev_tag:
        header = f"## [{new_version}]({repo_url}/compare/{prev_tag}...v{new_version}) ({date})\n"
    else:
        header = f"## {new_version} ({date})\n"

    parts = [header]
    if breaking:
        parts.append("\n### ⚠ BREAKING CHANGES\n\n" + "\n".join(breaking) + "\n")
    for key, title in SECTIONS:
        if buckets[key]:
            parts.append(f"\n### {title}\n\n" + "\n".join(buckets[key]) + "\n")

    sys.stdout.write("".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
