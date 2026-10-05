import json
import os
import re
from pathlib import Path
from typing import Any

from parrant.artifacts.adapter_mapping import normalize_adapter
from parrant.models.schema import TestNode

# Matches the quoted name(s) inside a dbt ``ref(...)`` expression, e.g.
# ``ref('stg_accounts')`` or ``ref('my_pkg', 'stg_accounts')``. The *last* quoted
# token is the model name (the first, when present, is the package).
_REF_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")


def _model_name_from_ref(ref_expr: str | None) -> str | None:
    """Extract the model name from a dbt ``ref(...)`` expression string.

    Returns ``None`` when nothing quoted can be found (e.g. a ``source(...)`` target
    or an unexpected shape) rather than guessing.
    """
    if not ref_expr:
        return None
    matches = _REF_QUOTED_RE.findall(ref_expr)
    if not matches:
        return None
    return matches[-1].lower()


def _model_name_from_unique_id(unique_id: str | None) -> str | None:
    """Return the lowercased model name from a ``model.<pkg>.<name>`` unique_id."""
    if not unique_id:
        return None
    parts = unique_id.split(".")
    if parts[0] != "model":
        return None
    return parts[-1].lower()


class ManifestReader:
    def __init__(self, manifest_path: str | None = None):
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.manifest: dict[str, Any] = {}
        # Lazily-built index of on-disk compiled SQL keyed by filename (e.g. ``orders.sql``),
        # used to recover a model's compiled SQL when the manifest's ``original_file_path``
        # has drifted from the ``target/compiled`` layout (a model moved between builds).
        self._compiled_index: dict[str, list[Path]] | None = None

    def load(self) -> None:
        if not self.manifest_path or not self.manifest_path.exists():
            raise FileNotFoundError(f"Manifest file not found: {self.manifest_path}")
        with open(self.manifest_path) as f:
            self.manifest = json.load(f)

    def get_adapter(self) -> str | None:
        adapter_name = self.manifest.get("metadata", {}).get("adapter_type")
        return normalize_adapter(adapter_name)

    def _find_node(self, model_name: str) -> dict[str, Any] | None:
        """Find a node in the manifest by model name."""
        if not self.manifest:
            return None
        model_name_lower = model_name.lower()
        for node in self.manifest.get("nodes", {}).values():
            if node.get("name", "").lower() == model_name_lower:
                return dict(node)
        return None

    def get_model_dependencies(self) -> dict[str, set[str]]:
        """Return a dictionary of model dependencies with full model names.

        Returns:
            Dict[str, Set[str]]: Key is full model name, value is set of full dependency names
        """
        dependencies = {}
        for model_id, model_data in self.manifest.get("nodes", {}).items():
            # `depends_on.nodes` is a list of unique_id strings (e.g. "model.pkg.name"),
            # not a list of dicts, so index it directly.
            depends_on = set(model_data.get("depends_on", {}).get("nodes", []))
            dependencies[model_id] = depends_on
        return dependencies

    def get_macro_dependents(self) -> dict[str, set[str]]:
        """Map each macro *file* to the model-like nodes whose compiled SQL it can affect.

        Keys are the macros' normalized ``original_file_path`` (e.g. ``macros/money.sql``);
        values are lowercased names of nodes (models/snapshots/seeds) that depend on a macro
        in that file — directly (``node.depends_on.macros``) or transitively (a macro that a
        used macro itself calls, via the ``macros[*].depends_on.macros`` graph). Feeds the
        ``--scope-git`` fail-safe: a changed macro file scopes to exactly these dependents.
        """
        macros = self.manifest.get("macros", {})
        macro_calls: dict[str, list[str]] = {
            macro_id: list(macro.get("depends_on", {}).get("macros", []))
            for macro_id, macro in macros.items()
        }

        # For each macro id, the closure of macros it (transitively) calls, itself included.
        # Iterative BFS with a visited set so macro-call cycles terminate.
        def _closure(macro_id: str) -> set[str]:
            seen: set[str] = set()
            stack = [macro_id]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(macro_calls.get(current, []))
            return seen

        def _norm(path: str) -> str:
            return re.sub(r"^\./", "", path.strip()).lstrip("/")

        dependents: dict[str, set[str]] = {
            _norm(macro.get("original_file_path") or ""): set()
            for macro in macros.values()
            if macro.get("original_file_path")
        }
        for node in self.manifest.get("nodes", {}).values():
            if node.get("resource_type") not in ("model", "snapshot", "seed"):
                continue
            node_name = (node.get("name") or "").lower()
            if not node_name:
                continue
            for macro_id in node.get("depends_on", {}).get("macros", []):
                for reached in _closure(macro_id):
                    path = (macros.get(reached) or {}).get("original_file_path")
                    if path:
                        dependents[_norm(path)].add(node_name)
        return dependents

    def get_model_upstream(self) -> dict[str, set[str]]:
        """Get upstream dependencies for each model."""
        upstream: dict[str, set[str]] = {}

        for node in self.manifest.get("nodes", {}).values():
            resource_type = node.get("resource_type")
            if resource_type in ("model", "snapshot"):
                model_name = node.get("name")
                if not model_name:
                    continue

                model_name = model_name.lower()
                upstream[model_name] = set()

                depends_on = node.get("depends_on", {})
                for dep_id in depends_on.get("nodes", []):
                    parts = dep_id.split(".")
                    if parts[0] == "model":
                        dep_name = parts[-1].lower()
                        upstream[model_name].add(dep_name)
                    elif parts[0] == "source":
                        source_node = self.manifest.get("sources", {}).get(dep_id, {})
                        source_identifier = source_node.get("identifier")
                        if source_identifier:
                            upstream[model_name].add(source_identifier.lower())
                        else:
                            # Fallback to source name if identifier not found
                            source_name = parts[-1].lower()
                            upstream[model_name].add(source_name)
                    elif parts[0] in ("snapshot", "seed"):
                        # Seeds are upstream nodes like any other: without this edge a model
                        # ref()ing a seed records no dependency, so the seed's consumers are
                        # invisible to reachability and to the rebuild selection.
                        dep_name = parts[-1].lower()
                        upstream[model_name].add(dep_name)

        return upstream

    def get_model_downstream(self) -> dict[str, set[str]]:
        """Return a dictionary of model downstream dependencies."""
        downstream: dict[str, set[str]] = {}

        upstream_deps = self.get_model_upstream()

        for model_name, upstream_models in upstream_deps.items():
            for upstream_model in upstream_models:
                if upstream_model not in downstream:
                    downstream[upstream_model] = set()
                downstream[upstream_model].add(model_name)

        return downstream

    def _resolve_compiled_file(self, node: dict[str, Any]) -> Path | None:
        """Locate the on-disk compiled SQL file for a node.

        Many real manifests are produced without embedded ``compiled_code`` (e.g.
        ``dbt parse`` or ``dbt docs generate`` without a compile step). In that case
        the compiled SQL still lives under ``target/compiled/**`` on disk, so we
        reconstruct its path from the manifest location and node metadata.
        """
        if not self.manifest_path:
            return None

        target_dir = self.manifest_path.parent
        project_root = target_dir.parent

        candidates = []

        # dbt records ``compiled_path`` relative to the project root once compiled.
        compiled_path = node.get("compiled_path")
        if compiled_path:
            candidates.append(project_root / compiled_path)
            candidates.append(Path(compiled_path))

        # dbt convention: <target>/compiled/<package_name>/<original_file_path>
        package_name = node.get("package_name")
        original_file_path = node.get("original_file_path")
        if package_name and original_file_path:
            candidates.append(target_dir / "compiled" / package_name / original_file_path)

        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue

        # Fallback: the exact path missed, but the compiled file may still be on disk under
        # a different sub-path — the manifest's ``original_file_path`` can drift from the
        # ``target/compiled`` layout when a model was moved/refactored between the build that
        # produced the manifest and the one that produced ``compiled/``. Recover it by the
        # compiled filename (dbt names it ``<model>.sql``), but ONLY when the match is
        # unambiguous, so we never silently attach the wrong (or stale-duplicate) SQL.
        if original_file_path:
            return self._recover_compiled_by_name(Path(original_file_path).name, package_name)
        return None

    def _recover_compiled_by_name(self, filename: str, package_name: str | None) -> Path | None:
        """Find an on-disk compiled file by its ``<model>.sql`` name, unambiguously.

        Prefers a single match under the model's own package dir; otherwise accepts a single
        match anywhere under ``target/compiled``. Returns ``None`` on zero or multiple matches
        (ambiguous → we decline to guess, keeping the model honestly unresolved).
        """
        index = self._compiled_basename_index()
        matches = index.get(filename, [])
        if not matches:
            return None
        if package_name:
            marker = f"{os.sep}compiled{os.sep}{package_name}{os.sep}"
            scoped = [p for p in matches if marker in f"{os.sep}{p}{os.sep}"]
            if len(scoped) == 1:
                return scoped[0]
        return matches[0] if len(matches) == 1 else None

    def _compiled_basename_index(self) -> dict[str, list[Path]]:
        """Lazily index ``target/compiled/**/*.sql`` by filename → list of paths."""
        if self._compiled_index is not None:
            return self._compiled_index
        index: dict[str, list[Path]] = {}
        if self.manifest_path:
            compiled_dir = self.manifest_path.parent / "compiled"
            if compiled_dir.is_dir():
                for path in compiled_dir.rglob("*.sql"):
                    index.setdefault(path.name, []).append(path)
        self._compiled_index = index
        return index

    def get_compiled_sql(self, model_name: str) -> str | None:
        """Get compiled SQL for a model.

        Prefers SQL embedded in the manifest, falling back to the compiled file on
        disk when the manifest was produced without embedded compiled code.
        """
        node = self._find_node(model_name)
        if not node:
            return None

        embedded = node.get("compiled_sql") or node.get("compiled_code")
        if embedded:
            return embedded

        compiled_file = self._resolve_compiled_file(node)
        if compiled_file:
            try:
                return compiled_file.read_text()
            except OSError:
                return None

        return None

    @staticmethod
    def _merged_meta(
        top_meta: dict[str, Any] | None, config_meta: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Merge a node's two dbt meta locations, ``config.meta`` winning over top-level.

        dbt exposes user-authored meta at both ``node.meta`` (legacy) and
        ``node.config.meta`` (canonical in dbt 1.x). When a key is present in both, the
        ``config`` value is authoritative (it is what dbt itself resolves). Neither
        present yields an empty dict — meta is *absent*, never guessed.
        """
        merged: dict[str, Any] = {}
        if isinstance(top_meta, dict):
            merged.update(top_meta)
        if isinstance(config_meta, dict):
            merged.update(config_meta)
        return merged

    def get_model_meta(self, model_name: str) -> dict[str, Any]:
        """Merged user-authored dbt ``meta`` for a model (``config.meta`` over ``meta``).

        This is arbitrary consumer metadata — ANY key an author declared — captured
        generically; no key is privileged. Returns an empty dict for an unknown model or
        one with no meta.
        """
        node = self._find_node(model_name)
        if not node:
            return {}
        config = node.get("config") or {}
        return self._merged_meta(node.get("meta"), config.get("meta"))

    def get_model_config(self, model_name: str) -> dict[str, Any]:
        """The node's resolved dbt ``config`` dict for a model (``node.config``).

        This is the generic dbt config surface — ``grants``, ``materialized``, ``tags``,
        ``enabled``, ``schema`` … — captured verbatim, exactly as dbt resolved it. Values
        are surfaced RAW (never normalized); no key is privileged. Returns an empty dict for
        an unknown model or one with no config — config is *absent*, never guessed.
        """
        node = self._find_node(model_name)
        if not node:
            return {}
        config = node.get("config") or {}
        return dict(config) if isinstance(config, dict) else {}

    def get_column_meta(self, model_name: str) -> dict[str, dict[str, Any]]:
        """Per-column merged user meta for a model, keyed by lowercased column name.

        Each column's meta merges ``columns.<c>.config.meta`` over ``columns.<c>.meta``
        (config wins), mirroring :meth:`get_model_meta`. Column keys are lowercased to
        match the codebase's case-insensitive column naming. Returns an empty dict for an
        unknown model; a declared column with no meta maps to an empty dict.
        """
        node = self._find_node(model_name)
        if not node:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for col_name, col_data in (node.get("columns") or {}).items():
            col_data = col_data or {}
            col_config = col_data.get("config") or {}
            result[col_name.lower()] = self._merged_meta(
                col_data.get("meta"), col_config.get("meta")
            )
        return result

    def get_model_path(self, model_name: str) -> str | None:
        """Get the path to the model from the manifest."""
        node = self._find_node(model_name)
        if not node:
            return None

        return node.get("path")

    def get_model_language(self, model_name: str) -> str | None:
        """Get the language of a model from the manifest."""
        node = self._find_node(model_name)
        if not node:
            return None
        return node.get("language")

    def get_model_resource_path(self, model_name: str) -> str | None:
        """Get the original file path of a model from the manifest."""
        node = self._find_node(model_name)
        if not node:
            return None
        return node.get("original_file_path")

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        node = self.manifest.get("nodes", {}).get(node_id)
        if node is None:
            return None
        return dict(node)

    def get_tests(self) -> list[TestNode]:
        """Read dbt test nodes (``resource_type == "test"``) from the manifest.

        We never run the tests; we read what they *declare*. For each test we extract:
        its ``unique_id``; the test kind (``test_metadata.name`` — not_null / unique /
        relationships / ...); the target column (top-level ``column_name``, falling back
        to ``test_metadata.kwargs.column_name``); the target model (from ``attached_node``,
        falling back to the sole model in ``depends_on.nodes``); for ``relationships``
        tests the referenced model/column (``kwargs.to`` / ``kwargs.field``); and the
        ``original_file_path``.

        Tests whose target column or model cannot be attributed are kept with the
        unknown field set to ``None`` (never guessed), so the reverse index can report
        coverage honestly.
        """
        tests: list[TestNode] = []

        for node_id, node in self.manifest.get("nodes", {}).items():
            if node.get("resource_type") != "test":
                continue

            test_metadata = node.get("test_metadata") or {}
            test_name = test_metadata.get("name")
            if not test_name:
                # Singular / custom SQL tests carry no ``test_metadata`` and no declared
                # column target. They CAN break on a column removal, but we can't know which
                # columns they touch without parsing the test SQL, so they're out of scope
                # for the column-level index — an unavoidable blind spot, not a safe one.
                continue

            kwargs = test_metadata.get("kwargs") or {}

            target_column = node.get("column_name") or kwargs.get("column_name")
            if isinstance(target_column, str):
                target_column = target_column.lower()
            else:
                target_column = None

            target_model = _model_name_from_unique_id(node.get("attached_node"))
            if target_model is None:
                model_deps = [
                    _model_name_from_unique_id(dep)
                    for dep in node.get("depends_on", {}).get("nodes", [])
                ]
                model_deps = [m for m in model_deps if m is not None]
                # Only attribute when unambiguous. A ``relationships`` test depends on
                # two models, so without ``attached_node`` we cannot tell which side is
                # the target — leave it unknown rather than guess.
                if len(model_deps) == 1:
                    target_model = model_deps[0]

            referenced_model: str | None = None
            referenced_column: str | None = None
            if test_name == "relationships":
                referenced_model = _model_name_from_ref(kwargs.get("to"))
                field = kwargs.get("field")
                if isinstance(field, str):
                    referenced_column = field.lower()

            tests.append(
                TestNode(
                    unique_id=node.get("unique_id") or node_id,
                    test_name=test_name,
                    target_model=target_model,
                    target_column=target_column,
                    referenced_model=referenced_model,
                    referenced_column=referenced_column,
                    resource_path=node.get("original_file_path"),
                )
            )

        return tests

    def get_exposures(self) -> dict[str, dict[str, Any]]:
        """Get all exposures from the manifest.

        Returns:
            Dict[str, Dict[str, Any]]: Key is exposure unique_id, value is exposure data
        """
        return self.manifest.get("exposures", {})

    def get_exposure_dependencies(self) -> dict[str, set[str]]:
        """Get model dependencies for each exposure.

        Returns:
            Dict[str, Set[str]]: Key is exposure name, value is set of model names it depends on
        """
        exposure_deps: dict[str, set[str]] = {}

        for exposure_data in self.manifest.get("exposures", {}).values():
            exposure_name = exposure_data.get("name")
            if not exposure_name:
                continue

            exposure_deps[exposure_name] = set()

            depends_on = exposure_data.get("depends_on", {})
            for dep_id in depends_on.get("nodes", []):
                parts = dep_id.split(".")
                if parts[0] == "model":
                    dep_name = parts[-1].lower()
                    exposure_deps[exposure_name].add(dep_name)
                elif parts[0] == "source":
                    source_node = self.manifest.get("sources", {}).get(dep_id, {})
                    source_identifier = source_node.get("identifier")
                    if source_identifier:
                        exposure_deps[exposure_name].add(source_identifier.lower())
                    else:
                        source_name = parts[-1].lower()
                        exposure_deps[exposure_name].add(source_name)
                elif parts[0] == "snapshot":
                    dep_name = parts[-1].lower()
                    exposure_deps[exposure_name].add(dep_name)

        return exposure_deps

    def get_model_exposures(self) -> dict[str, set[str]]:
        """Get exposures that depend on each model.

        Returns:
            Dict[str, Set[str]]: Key is model name, value is set of exposure names that depend on it
        """
        model_exposures: dict[str, set[str]] = {}

        exposure_deps = self.get_exposure_dependencies()

        for exposure_name, model_names in exposure_deps.items():
            for model_name in model_names:
                if model_name not in model_exposures:
                    model_exposures[model_name] = set()
                model_exposures[model_name].add(exposure_name)

        return model_exposures
