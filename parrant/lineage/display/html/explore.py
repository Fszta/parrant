import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from parrant.models.schema import Column, ColumnLineage, TestNode

if TYPE_CHECKING:
    from parrant.lineage.service import LineageService

logger = logging.getLogger(__name__)


class ColumnInfo(BaseModel):
    name: str
    model: str
    type: str | None = None
    description: str | None = None


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    model: str
    data_type: str | None = None
    is_main: bool = False
    resource_type: str | None = None
    is_key: bool = False
    # For row-set (filter/join/QUALIFY) dependents: the predicate the upstream column appears
    # in — the "why" behind a node that consumes the column without projecting its value.
    note: str | None = None
    # dbt tests (not_null / unique / relationships / ...) declared on this column — the
    # guardrails a reviewer wants to see at a glance. Empty list = untested; None until enriched.
    tests: list[dict[str, Any]] | None = None
    # change-context marks (append-only; None in pure-explore mode → today's payload
    # byte-for-byte). ``semantic`` is the AST-diff class of a CHANGED column node
    # (equivalent|meaning_changed|indeterminate); ``breaking`` is the fail-safe convenience
    # (anything not proven equivalent, plus removed/type_changed); ``boundary`` tags a node
    # that sits past the dbt edge (e.g. "metabase") for the graph's BI band.
    semantic: str | None = None
    breaking: bool | None = None
    boundary: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str = "lineage"
    # the single amber blast-path edge — set when the edge leaves a breaking column.
    breaking: bool | None = None


class GraphData(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    main_node: str | None = None
    column_info: ColumnInfo | None = None
    impact_summary: dict[str, Any] | None = None


class LineageExplorer:
    """Interactive server for exploring column lineage."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8000):
        self.app = FastAPI()
        self.host = host
        self.port = port
        self.data = GraphData()
        self.lineage_service: LineageService | None = None
        self._start_model: str | None = None
        self._start_column: str | None = None

        # change context (optional). Populated by ``set_change_context`` with the
        # already-computed changeset report (semantic per changed column, policy verdict,
        # cross-boundary Metabase reach, coverage honesty). All None here => pure-explore
        # mode, and every endpoint renders exactly today's payload.
        self._change_report: dict[str, Any] | None = None
        self._policy_verdict: dict[str, Any] | None = None
        self._policy_decision: str | None = None
        self._metabase_coverage: dict[str, Any] | None = None
        # (model, column) -> {"semantic": str|None, "breaking": bool} for the CHANGED columns.
        self._change_by_column: dict[tuple[str, str], dict[str, Any]] = {}
        # name -> full metabase exposure entry (source/precision/via_cards/meta).
        self._metabase_exposures: dict[str, dict[str, Any]] = {}
        # (model, column) -> the metabase dashboards THIS change reaches, each as
        # {name, via_columns, precision} so per-change reach stays column-precise (F4).
        self._metabase_reach_by_change: dict[tuple[str, str], list[dict[str, Any]]] = {}
        # A MetabaseReach index for STATIC cross-boundary exploration (no changeset needed):
        # "what Metabase cards/dashboards read this column?" is a lineage question, not an
        # impact question, so it should answer for any browsed column. Populated on demand
        # per explored column in pure-explore mode; the changeset path takes precedence.
        self._metabase_static_reach: Any | None = None
        self._metabase_dashboard_names: dict[int, str] = {}

        self._setup_templates_and_routes()

    @staticmethod
    def _is_breaking(kind: str | None, semantic: str | None) -> bool:
        """Fail-safe breaking flag for a changed column.

        Everything is breaking except a purely additive column or a proven-equivalent
        logic change: ``removed`` / ``type_changed`` break structurally; a logic change
        breaks unless its semantic is ``equivalent``; an unknown/``indeterminate`` semantic
        is treated as breaking (never render "unknown" as "safe").
        """
        if kind == "added":
            return False
        return semantic != "equivalent"

    def set_change_context(self, report: dict[str, Any] | None) -> None:
        """Attach an already-computed changeset report so the explorer can surface the
        product signals (semantic categorization, policy verdict, Metabase reach, coverage).

        Strictly additive: passing ``None`` (or omitting the call) leaves the explorer in
        pure-explore mode where every endpoint renders exactly today's payload. The report
        shape is ``build_changeset_report(...)`` + ``policy_verdict`` / ``metabase`` blocks
        as assembled by the ``impact`` CLI command — this method only indexes it, never
        recomputes lineage.
        """
        # Reset so a second call fully replaces the prior context.
        self._change_report = report
        self._policy_verdict = None
        self._policy_decision = None
        self._metabase_coverage = None
        self._change_by_column = {}
        self._metabase_exposures = {}
        self._metabase_reach_by_change = {}
        if not report:
            return

        verdict = report.get("policy_verdict")
        if isinstance(verdict, dict):
            self._policy_verdict = verdict
            decision = verdict.get("decision")
            self._policy_decision = decision if isinstance(decision, str) else None

        metabase = report.get("metabase")
        if isinstance(metabase, dict):
            self._metabase_coverage = metabase

        for entry in report.get("affected_exposures", []) or []:
            if isinstance(entry, dict) and entry.get("source") == "metabase":
                name = entry.get("name")
                if isinstance(name, str):
                    self._metabase_exposures[name] = entry

        for change in report.get("by_change", []) or []:
            if not isinstance(change, dict):
                continue
            model = change.get("model")
            column = change.get("column")
            if not isinstance(model, str) or not isinstance(column, str):
                continue
            key = (model, column)
            self._change_by_column[key] = {
                "semantic": change.get("semantic"),
                "breaking": self._is_breaking(change.get("kind"), change.get("semantic")),
            }
            reached = [
                {
                    "name": exposure.get("name"),
                    "via_columns": exposure.get("via_columns") or [],
                    "precision": exposure.get("precision"),
                }
                for exposure in change.get("reached_exposures", []) or []
                if isinstance(exposure, dict) and exposure.get("name") in self._metabase_exposures
            ]
            if reached:
                self._metabase_reach_by_change[key] = reached

    def _change_mark(self, model: Any, column: Any) -> dict[str, Any] | None:
        """The semantic/breaking mark for ``model.column`` if it is a changed column."""
        if not isinstance(model, str) or not isinstance(column, str):
            return None
        return self._change_by_column.get((model, column))

    def _enrich_impact_with_change_context(
        self, impact_data: dict[str, Any], model: str, column: str
    ) -> dict[str, Any]:
        """Fold the change-context signals onto a single-column impact payload.

        Additive and guarded: with no change context (pure-explore mode) or a non-dict
        payload (an error), the payload is returned untouched. Decorates each downstream
        affected column with its ``semantic`` / ``breaking`` class when it is itself a
        changed column, marks the subject column, appends the Metabase dashboards this
        change reaches (with precision / via_cards / meta), and attaches the whole-change
        ``policy_verdict``.
        """
        if not isinstance(impact_data, dict) or self._change_report is None:
            return impact_data

        for affected in impact_data.get("affected_columns", []):
            if not isinstance(affected, dict):
                continue
            mark = self._change_mark(affected.get("model"), affected.get("column"))
            if mark is not None:
                affected["semantic"] = mark["semantic"]
                affected["breaking"] = mark["breaking"]

        subject = self._change_by_column.get((model, column))
        if subject is not None:
            impact_data["subject_semantic"] = subject["semantic"]
            impact_data["subject_breaking"] = subject["breaking"]

        # Append the Metabase dashboards THIS change reaches — column-accurate (read from the
        # change's own reached set), never a blanket dump of every dashboard in the run.
        exposures = impact_data.setdefault("affected_exposures", [])
        if isinstance(exposures, list):
            present = {e.get("name") for e in exposures if isinstance(e, dict)}
            for reached in self._metabase_reach_by_change.get((model, column), []):
                name = reached.get("name")
                if not isinstance(name, str):
                    continue
                base = self._metabase_exposures.get(name)
                if base is None:
                    continue
                # Per-change scoped copy: precisely which column(s) of this dashboard THIS
                # change touches (F4), not the union across the whole PR that the global
                # exposure entry carries.
                entry = dict(base)
                entry["via_columns"] = reached.get("via_columns") or []
                if reached.get("precision"):
                    entry["precision"] = reached["precision"]
                if name in present:
                    # Already listed (dbt side): merge the cross-boundary provenance fields.
                    for existing in exposures:
                        if isinstance(existing, dict) and existing.get("name") == name:
                            existing.update(entry)
                            break
                else:
                    exposures.append(entry)
                    present.add(name)

        # The policy verdict is CHANGE-SCOPED: it is the whole-change gate decision, and it
        # only means something for a column that is actually part of the reviewed change.
        # Merely *exploring* a column is not making a change and carries no verdict — attaching
        # the global verdict to any selected column would misread "I'm inspecting this column"
        # as "a change to it blocks". Surface an explicit membership flag so the
        # UI can say "not part of this change" instead of showing a stale global block.
        impact_data["subject_in_changeset"] = subject is not None
        if subject is not None and self._policy_verdict is not None:
            impact_data["policy_verdict"] = self._policy_verdict

        return impact_data

    def _setup_templates_and_routes(self) -> None:
        """Setup templates, static files, and routes."""
        self.templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
        self.app.mount(
            "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
        )

        @self.app.get("/", response_class=HTMLResponse)
        async def home(request: Request) -> Any:
            return self.templates.TemplateResponse(
                request=request,
                name="graph.html",
                context={"data": GraphData().model_dump(), "explore_mode": True},
            )

        @self.app.get("/api/graph")
        async def get_graph_data() -> dict[str, Any]:
            return self.data.model_dump()

        @self.app.get("/api/coverage")
        async def get_coverage() -> dict[str, Any]:
            if not self.lineage_service:
                return {"error": "Lineage service not initialized"}
            coverage = self.lineage_service.get_coverage().model_dump()
            # Cross-boundary honesty: when a Metabase artifact was joined, append its
            # reach-confidence block so the coverage footer can be honest about BI reach too.
            # Absent when the feature is off → backward compatible.
            if self._metabase_coverage is not None:
                coverage["metabase"] = self._metabase_coverage
            return coverage

        @self.app.get("/api/policy-verdict")
        async def get_policy_verdict() -> dict[str, Any]:
            # Change-wide decision (block/warn/allow), independent of the selected column.
            # ``{"decision": None}`` when no policy resolved / no changeset — the frontend
            # treats that as "feature not active" and renders nothing new.
            if self._policy_verdict is not None:
                return self._policy_verdict
            return {"decision": None}

        @self.app.get("/api/models")
        async def get_models() -> list[dict[str, Any]]:
            if not self.lineage_service:
                return []

            model_tree_root: list[dict[str, Any]] = []
            all_models = self.lineage_service.registry.get_models()
            all_exposures = self.lineage_service.registry.get_exposures()

            def insert_into_tree(
                tree: list[dict[str, Any]], path_parts: list[str], model_data: dict[str, Any]
            ) -> None:
                current_level = tree
                for i, part in enumerate(path_parts):
                    is_last_part = i == len(path_parts) - 1
                    node_type = "model" if is_last_part else "folder"

                    node = next(
                        (
                            item
                            for item in current_level
                            if item["name"] == part and item["type"] == node_type
                        ),
                        None,
                    )

                    if node is None:
                        node = {"name": part, "type": node_type}
                        if node_type == "folder":
                            node["children"] = []
                        else:
                            node["model_name"] = model_data.get(
                                "model_name", model_data["registry_key"]
                            )
                            node["display_name"] = part
                            node["columns"] = model_data.get("columns", [])
                            node["resource_type"] = model_data["resource_type"]
                            if "description" in model_data:
                                node["description"] = model_data["description"]
                            if "exposure_data" in model_data:
                                node["exposure_data"] = model_data["exposure_data"]

                        current_level.append(node)
                        current_level.sort(key=lambda x: (x["type"] != "folder", x["name"]))
                    elif node_type != "folder" and is_last_part:
                        if "model_name" in model_data:
                            node["model_name"] = model_data["model_name"]
                        node["display_name"] = part
                        if "columns" in model_data:
                            node["columns"] = model_data["columns"]
                        if "resource_type" in model_data:
                            node["resource_type"] = model_data["resource_type"]

                    if node_type == "folder":
                        if "children" not in node:
                            node["children"] = []
                        current_level = node["children"]

            for model_registry_key, model in all_models.items():
                resource_path_str = getattr(model, "resource_path", None)
                resource_type = getattr(model, "resource_type", None)
                model_name_for_path = model_registry_key

                if resource_type == "source":
                    source_name = getattr(model, "source_name", None)
                    if source_name:
                        path_parts = ["source", source_name, model_registry_key]
                    else:
                        path_parts = ["source", model_registry_key]
                elif resource_path_str:
                    p = Path(resource_path_str)
                    path_parts = list(p.parent.parts) + [model_name_for_path]
                else:
                    path_parts = [model_name_for_path]

                path_parts = [part for part in path_parts if part]
                if not path_parts:
                    path_parts = [model_name_for_path]

                columns = [
                    {"name": col_name, "type": col.data_type, "description": col.description}
                    for col_name, col in model.columns.items()
                ]

                model_data_payload = {
                    "registry_key": model_registry_key,
                    "model_name": model_registry_key,
                    "columns": columns,
                    "resource_type": resource_type,
                    "description": getattr(model, "description", None),
                }

                insert_into_tree(model_tree_root, path_parts, model_data_payload)

            # Add exposures to the tree
            for exposure_name, exposure in all_exposures.items():
                exposure_data_payload = {
                    "registry_key": exposure_name,
                    "columns": [],
                    "resource_type": "exposure",
                    "exposure_data": {
                        "name": exposure.name,
                        "type": exposure.type,
                        "url": exposure.url,
                        "description": exposure.description,
                        "owner": exposure.owner,
                        "depends_on_models": list(exposure.depends_on_models),
                    },
                }

                # Group exposures under an "exposures" folder
                path_parts = ["exposures", exposure_name]
                insert_into_tree(model_tree_root, path_parts, exposure_data_payload)

            return model_tree_root

        @self.app.get("/api/lineage/{model}/{column}")
        async def get_lineage(model: str, column: str) -> dict[str, Any]:
            if not self.lineage_service:
                return {"error": "Lineage service not initialized"}

            try:
                self.data = GraphData()
                model_obj = self.lineage_service.registry.get_model(model)
                column_obj = model_obj.columns.get(column)

                if not column_obj:
                    return {"error": f"Column {column} not found in model {model}"}

                self._set_column_info(column_obj)
                self.data.main_node = f"col_{model}_{column}"
                self._process_lineage_tree(model, column)
                # Store starting model/column for exposure edge creation
                self._start_model = model
                self._start_column = column

                # Get impact summary for the relationship summary card
                try:
                    impact_data = self.lineage_service.get_column_impact(model, column)
                    if impact_data and "summary" in impact_data:
                        summary = dict(impact_data["summary"])
                        # Carry the confidence block alongside the summary metrics so the
                        # relationship summary card can surface it next to the tiles.
                        summary["confidence"] = impact_data.get("confidence")
                        # thread the whole-change policy decision so the summary card can
                        # show a pip (and the graph its subject ring) without a second fetch.
                        # CHANGE-SCOPED: only when the explored column is itself
                        # part of the reviewed change — otherwise the graph would ring an
                        # arbitrary explored column with the global verdict. Only added when a
                        # policy resolved → pure-explore payload stays byte-for-byte unchanged.
                        if self._policy_decision is not None and self._change_mark(model, column):
                            summary["policy_decision"] = self._policy_decision
                        self.data.impact_summary = summary
                    else:
                        self.data.impact_summary = None
                except Exception as e:
                    logger.debug(f"Could not get impact summary for {model}.{column}: {e}")
                    # Don't fail the lineage request if impact analysis fails
                    self.data.impact_summary = None

                return self.data.model_dump(exclude_none=False)
            except Exception as e:
                import traceback

                logger.error(f"Error getting lineage: {e}")
                logger.debug(traceback.format_exc())
                return {"error": str(e)}

        @self.app.get("/api/model/{model_name}/details")
        async def get_model_details(model_name: str) -> dict[str, Any]:
            if not self.lineage_service:
                return {"error": "Lineage service not initialized"}

            try:
                model = self.lineage_service.registry.get_model(model_name)
                return {
                    "name": model.name,
                    "description": model.description,
                    "tags": model.tags,
                    "resource_type": model.resource_type,
                    "schema": model.schema_name,
                    "database": model.database,
                }
            except Exception as e:
                return {"error": str(e)}

        @self.app.get("/api/impact-analysis/{model}/{column}")
        async def get_impact_analysis(model: str, column: str) -> dict[str, Any]:
            if not self.lineage_service:
                return {"error": "Lineage service not initialized"}

            try:
                # Verify model exists
                try:
                    model_obj = self.lineage_service.registry.get_model(model)
                except (ValueError, KeyError) as e:
                    return {"error": f"Model '{model}' not found: {e!s}"}

                # Verify column exists
                if column not in model_obj.columns:
                    return {"error": f"Column '{column}' not found in model '{model}'"}

                impact_data = self.lineage_service.get_column_impact(model, column)
                impact_data = self._enrich_impact_with_tests(impact_data)
                return self._enrich_impact_with_change_context(impact_data, model, column)
            except ValueError as e:
                # Handle specific value errors (e.g., model/column not found)
                logger.warning(f"Impact analysis error for {model}.{column}: {e}")
                return {"error": str(e)}
            except Exception as e:
                import traceback

                logger.error(f"Error getting impact analysis for {model}.{column}: {e}")
                logger.debug(traceback.format_exc())
                return {"error": str(e)}

    def _process_lineage_tree(self, start_model: str, start_column: str) -> None:
        """Process complete lineage tree from starting point."""
        if not self.lineage_service:
            return

        try:
            self._start_model = start_model
            self._start_column = start_column

            start_col_node_id = f"col_{start_model}_{start_column}"
            if not any(n["id"] == start_col_node_id for n in self.data.nodes):
                try:
                    model_obj = self.lineage_service.registry.get_model(start_model)
                    column_obj = model_obj.columns.get(start_column)
                    if column_obj:
                        self._set_column_info(column_obj)
                        self.data.main_node = start_col_node_id
                except Exception:
                    pass

            upstream_refs = self.lineage_service._get_upstream_lineage(start_model, start_column)
            downstream_refs = self.lineage_service._get_downstream_lineage(
                start_model, start_column
            )

            main_node_id = self.data.main_node or start_col_node_id

            self._enrich_nodes_with_metadata([upstream_refs, downstream_refs])
            self._add_processed_data(upstream_refs, "upstream", main_node_id)
            self._add_processed_data(downstream_refs, "downstream")
            self._add_rowset_dependents()
            self._attach_column_tests()
            self._annotate_nodes_with_semantic()
            self._populate_static_reach(start_model, start_column)
            self._annotate_boundary_nodes()

        except Exception as e:
            logger.error(f"Error processing lineage for {start_model}.{start_column}: {e}")

    def _annotate_nodes_with_semantic(self) -> None:
        """Mark changed column nodes (and their outgoing edges) with the semantic class.

        A single post-pass keeps the many node/edge creation sites untouched. No-op in
        pure-explore mode (empty change context) → the graph payload is unchanged.
        """
        if not self._change_by_column:
            return
        breaking_ids: set[str] = set()
        for node in self.data.nodes:
            if node.get("type") != "column":
                continue
            mark = self._change_mark(node.get("model"), node.get("label"))
            if mark is None:
                continue
            node["semantic"] = mark["semantic"]
            node["breaking"] = mark["breaking"]
            if mark["breaking"]:
                breaking_ids.add(node["id"])
        # The blast-path edge (DESIGN.md's one warm edge): an edge leaving a breaking column.
        for edge in self.data.edges:
            if edge.get("source") in breaking_ids:
                edge["breaking"] = True

    @staticmethod
    def _boundary_exposure_data(entry: dict[str, Any]) -> dict[str, Any]:
        """The graph ``exposure_data`` payload for a reached Metabase dashboard node."""
        exposure_data: dict[str, Any] = {
            "boundary": "metabase",
            "type": entry.get("type") or "dashboard",
        }
        if entry.get("precision") is not None:
            exposure_data["precision"] = entry.get("precision")
        if entry.get("via_cards") is not None:
            exposure_data["via_cards"] = entry.get("via_cards")
        # F4: the column-precise chain (which dbt column -> card field the change hits), so the
        # dashboard node/card can name the affected field instead of the whole board.
        if entry.get("via_columns"):
            exposure_data["via_columns"] = entry.get("via_columns")
        meta = entry.get("meta")
        if isinstance(meta, dict) and meta.get("tier") is not None:
            exposure_data["tier"] = meta.get("tier")
        if entry.get("url"):
            exposure_data["url"] = entry.get("url")
        return exposure_data

    def attach_metabase_static(
        self, reach: Any, dashboard_names: dict[int, str] | None = None
    ) -> None:
        """Attach a :class:`MetabaseReach` for STATIC cross-boundary exploration.

        Unlike :meth:`set_change_context` (which only surfaces reach for *changed* columns),
        this lets the explorer answer "which Metabase cards/dashboards read this column?" for
        ANY browsed column — a lineage question that needs no change. The changeset path still
        takes precedence when a change context is loaded. ``dashboard_names`` maps a Metabase
        dashboard id to its human name, so injected nodes read "Revenue KPIs" not
        "metabase.dashboard.137"."""
        self._metabase_static_reach = reach
        self._metabase_dashboard_names = dashboard_names or {}

    def _populate_static_reach(self, model: str, column: str) -> None:
        """In pure-explore mode, compute the reached Metabase dashboards for the browsed
        column from the static reach index and stage them exactly like the changeset path,
        so :meth:`_annotate_boundary_nodes` injects them with no other change."""
        if self._metabase_static_reach is None or self._change_report is not None:
            return
        try:
            entries = self._metabase_static_reach.reached_dashboards(
                columns=[(model, column.lower())], models=[model]
            )
        except Exception:  # reach is best-effort; never break the graph over it
            return
        reach_list: list[dict[str, Any]] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            # Resolve a human label ("metabase.dashboard.137" -> its real name) for display.
            try:
                did = int(name.rsplit(".", 1)[-1])
                human = self._metabase_dashboard_names.get(did)
                if human:
                    entry = {**entry, "_label": human}
            except ValueError:
                pass
            self._metabase_exposures[name] = entry
            reach_list.append(
                {
                    "name": name,
                    "via_columns": entry.get("via_columns") or [],
                    "precision": entry.get("precision"),
                }
            )
        if reach_list:
            self._metabase_reach_by_change[(model, column)] = reach_list

    def _annotate_boundary_nodes(self) -> None:
        """Surface reached Metabase dashboards as graph nodes past the dbt/BI boundary.

        Two passes, both no-ops without a joined Metabase artifact (``_metabase_exposures``
        empty) → the graph payload is byte-for-byte today's:

        1. **Tag** any exposure node whose name already matches a joined dashboard
           (``boundary="metabase"`` + provenance folded into ``exposure_data``).
        2. **Inject** the dashboards THIS explored change reaches but that the registry
           lineage walk never produced as nodes (they come from the cross-boundary reach
           join, not dbt's own graph). Each is anchored to the exact terminal dbt column the
           card reads when that column is carried on the reach entry (``via_columns``, F4) and
           its node exists in the graph — so the dashboard fans out of the precise field it
           consumes. Falls back to the downstream mart-leaf columns (then the main node) when
           no column-precise anchor is available (a table-grain reach).
        """
        if not self._metabase_exposures:
            return

        # Pass 1 — tag exposure nodes the registry already emitted that are dashboards.
        present_exposure_names: set[str] = set()
        for node in self.data.nodes:
            if node.get("type") != "exposure":
                continue
            label = node.get("label")
            if label is None:
                continue
            present_exposure_names.add(label)
            entry = self._metabase_exposures.get(label) or self._metabase_exposures.get(
                node.get("model") or ""
            )
            if entry is None:
                continue
            node["boundary"] = "metabase"
            exposure_data = node.get("exposure_data")
            if not isinstance(exposure_data, dict):
                exposure_data = {}
                node["exposure_data"] = exposure_data
            exposure_data.update(self._boundary_exposure_data(entry))

        # Pass 2 — inject the dashboards this change reaches that aren't graph nodes yet.
        reached = self._metabase_reach_by_change.get(
            (self._start_model or "", self._start_column or "")
        )
        if not reached:
            return

        leaf_anchors = self._downstream_mart_leaf_ids()
        if not leaf_anchors:
            main = self.data.main_node
            leaf_anchors = [main] if isinstance(main, str) else []
        if not leaf_anchors:
            return

        # All column-node ids currently in the graph, so a column-precise anchor can be
        # validated before use (fall back to the mart-leaf heuristic when the exact node
        # isn't laid out).
        node_ids = {n.get("id") for n in self.data.nodes if isinstance(n, dict)}

        for reached_entry in reached:
            name = reached_entry.get("name")
            if not isinstance(name, str) or name in present_exposure_names:
                continue
            base = self._metabase_exposures.get(name)
            if base is None:
                continue
            entry = dict(base)
            via_columns = reached_entry.get("via_columns") or []
            entry["via_columns"] = via_columns
            if reached_entry.get("precision"):
                entry["precision"] = reached_entry["precision"]
            node_id = f"exposure_{name}"
            # Display the human dashboard name when known (static path sets ``_label``); keep
            # ``model=name`` as the stable id-based key used for edges/dedup.
            display_label = base.get("_label") or name
            self.data.nodes.append(
                GraphNode(
                    id=node_id,
                    label=display_label,
                    type="exposure",
                    model=name,
                    resource_type="exposure",
                    boundary="metabase",
                ).model_dump()
            )
            # Attach exposure_data (dict, not a GraphNode field) after construction.
            self.data.nodes[-1]["exposure_data"] = self._boundary_exposure_data(entry)
            present_exposure_names.add(name)
            # Anchor to the exact terminal column node(s) the card reads (F4) when present;
            # else the mart-leaf heuristic. Dedupe so one column doesn't draw two edges.
            precise = [
                f"col_{v['model']}_{v['column']}"
                for v in via_columns
                if isinstance(v, dict) and v.get("model") and v.get("column")
            ]
            anchors = [a for a in dict.fromkeys(precise) if a in node_ids] or leaf_anchors
            for anchor_id in anchors:
                self.data.edges.append(
                    GraphEdge(source=anchor_id, target=node_id, type="exposure").model_dump()
                )

    def _downstream_mart_leaf_ids(self) -> list[str]:
        """The terminal downstream dbt-model columns — targets of a lineage edge that are not
        themselves a source of one — where the blast path exits into the BI layer. Snapshots
        and sources are excluded so a dashboard fans out of the marts, not a raw table."""
        lineage_sources: set[str] = set()
        lineage_targets: set[str] = set()
        for edge in self.data.edges:
            if edge.get("type", "lineage") != "lineage":
                continue
            source = edge.get("source")
            target = edge.get("target")
            if source is not None:
                lineage_sources.add(source)
            if target is not None:
                lineage_targets.add(target)
        by_id = {n.get("id"): n for n in self.data.nodes}
        leaves: list[str] = []
        for node_id in lineage_targets:
            if node_id in lineage_sources:
                continue
            node = by_id.get(node_id)
            if node is None or node.get("type") != "column":
                continue
            if node.get("resource_type") in ("snapshot", "source", "seed"):
                continue
            leaves.append(node_id)
        return sorted(leaves)

    @staticmethod
    def _serialize_test(test: TestNode) -> dict[str, Any]:
        """Flatten a :class:`TestNode` into the compact dict the frontend renders."""
        return {
            "test_name": test.test_name,
            "unique_id": test.unique_id,
            "resource_path": test.resource_path,
            "referenced_model": test.referenced_model,
            "referenced_column": test.referenced_column,
        }

    def _column_tests_payload(self, model: str | None, column: str | None) -> list[dict[str, Any]]:
        """The dbt tests declared on ``model.column`` (empty when untested/unknown).

        Reuses the registry's prebuilt reverse index — never re-parses artifacts.
        """
        if not self.lineage_service or not model or not column:
            return []
        try:
            tests = self.lineage_service.registry.get_column_tests(model, column)
        except Exception as e:
            logger.debug(f"Could not load tests for {model}.{column}: {e}")
            return []
        return [self._serialize_test(t) for t in tests]

    def _enrich_impact_with_tests(self, impact_data: dict[str, Any]) -> dict[str, Any]:
        """Attach the tests covering each affected column to an impact payload.

        Lets the impact panel show which guarantees a change threatens. Mutates and returns
        the payload; a non-dict (e.g. an error) is passed through untouched.
        """
        if isinstance(impact_data, dict):
            for affected in impact_data.get("affected_columns", []):
                if isinstance(affected, dict):
                    affected["tests"] = self._column_tests_payload(
                        affected.get("model"), affected.get("column")
                    )
        return impact_data

    def _attach_column_tests(self) -> None:
        """Annotate every column node in the graph with the tests covering it.

        A single post-pass over the assembled nodes keeps the (many) node-creation sites
        untouched — each ``column`` node gets a ``tests`` list the UI shows behind the toggle.
        """
        for node in self.data.nodes:
            if node.get("type") != "column":
                continue
            node["tests"] = self._column_tests_payload(node.get("model"), node.get("label"))

    def _add_rowset_dependents(self) -> None:
        """Add row-set (filter/join/QUALIFY) dependents as distinct nodes.

        A model can depend on a column without ever projecting its value — it uses it only in
        a WHERE / JOIN ON / HAVING / QUALIFY (incl. a window ORDER BY). Column-value lineage
        (and therefore the graph so far) misses these, yet the column change shifts their
        row-set. We surface each such consumer as one ``rowset`` node hanging off the
        value-carrying column it filters on, carrying the predicate text as its ``note``.
        """
        if not self.lineage_service:
            return
        registry = self.lineage_service.registry

        # Models already shown as value nodes — don't duplicate them as row-set nodes.
        value_models = {n["model"] for n in self.data.nodes if n.get("type") == "column"}
        # Snapshot the value column nodes now, so we iterate a stable list while appending.
        column_nodes = [n for n in self.data.nodes if n.get("type") == "column"]
        added: set = set()

        for node in column_nodes:
            src_key = f"{node['model']}.{node['label']}"
            try:
                dependents = sorted(registry.get_filter_dependents(src_key))
            except Exception:
                continue
            for dep_model in dependents:
                if dep_model in value_models or dep_model in added:
                    continue
                added.add(dep_model)
                try:
                    dep = registry.get_model(dep_model)
                    note = (getattr(dep, "predicate_lineage", {}) or {}).get(src_key)
                    resource_type = getattr(dep, "resource_type", "model")
                except Exception:
                    note, resource_type = None, "model"
                rowset_id = f"rowset_{dep_model}"
                self.data.nodes.append(
                    GraphNode(
                        id=rowset_id,
                        label=dep_model,
                        type="rowset",
                        model=dep_model,
                        resource_type=resource_type,
                        note=note,
                    ).model_dump()
                )
                self.data.edges.append(
                    GraphEdge(source=node["id"], target=rowset_id, type="rowset").model_dump()
                )

    def _enrich_nodes_with_metadata(
        self, refs_list: list[dict[str, dict[str, ColumnLineage] | set[str]]]
    ) -> None:
        """Enrich nodes with metadata like data types and resource types."""
        if not self.lineage_service:
            return

        for refs in refs_list:
            for model_name, columns in sorted(refs.items()):
                if model_name == "exposures" or not isinstance(columns, dict):
                    continue

                try:
                    model_obj = self.lineage_service.registry.get_model(model_name)
                    if not model_obj:
                        continue

                    resource_type = getattr(model_obj, "resource_type", None)

                    for col_name in sorted(columns.keys()):
                        col_obj = model_obj.columns.get(col_name)
                        if not col_obj:
                            continue

                        node_id = f"col_{model_name}_{col_name}"

                        found = False
                        for node in self.data.nodes:
                            if node["id"] == node_id:
                                node["data_type"] = col_obj.data_type
                                node["resource_type"] = resource_type
                                found = True
                                break

                        if not found:
                            self._add_node(
                                id=node_id,
                                label=col_name,
                                model=model_name,
                                data_type=col_obj.data_type,
                                resource_type=resource_type,
                            )
                except Exception as e:
                    logger.error(f"Error enriching node metadata for {model_name}: {e}")

    def _queue_additional_nodes(
        self,
        upstream_refs: dict[str, dict[str, ColumnLineage] | set[str]],
        downstream_refs: dict[str, dict[str, ColumnLineage] | set[str]],
        processed: set[tuple[str, str]],
        to_process: list[tuple[str, str]],
    ) -> None:
        """Queue additional nodes for processing in sorted order for deterministic BFS."""
        new_nodes: list[tuple[str, str]] = []
        for refs in [upstream_refs, downstream_refs]:
            for model_name, columns in sorted(refs.items()):
                if model_name == "exposures" or not isinstance(columns, dict):
                    continue

                for col_name in sorted(columns.keys()):
                    if (model_name, col_name) not in processed and (
                        model_name,
                        col_name,
                    ) not in to_process:
                        new_nodes.append((model_name, col_name))

        new_nodes.sort()
        to_process.extend(new_nodes)

    def _add_processed_data(
        self,
        refs: dict[str, dict[str, ColumnLineage] | set[str]],
        direction: str,
        main_node_id: str | None = None,
    ) -> None:
        """Process refs and add to graph."""
        processed = self._process_refs(refs, direction, main_node_id)

        for node in processed["nodes"]:
            existing_node = next((n for n in self.data.nodes if n["id"] == node["id"]), None)
            if existing_node:
                if "direction" in node and "direction" not in existing_node:
                    existing_node["direction"] = node["direction"]
                for key in ["data_type", "resource_type"]:
                    if key in node and (key not in existing_node or existing_node[key] is None):
                        existing_node[key] = node[key]
            else:
                self.data.nodes.append(node)

        for edge in processed["edges"]:
            if not any(
                e["source"] == edge["source"] and e["target"] == edge["target"]
                for e in self.data.edges
            ):
                self.data.edges.append(edge)

        if direction == "upstream" and main_node_id and self.lineage_service:
            try:
                if self._start_model and self._start_column:
                    model_obj = self.lineage_service.registry.get_model(self._start_model)
                    col_obj = model_obj.columns.get(self._start_column)
                    if col_obj and col_obj.lineage:
                        for lin in col_obj.lineage:
                            for source in lin.source_columns:
                                split_result = self._split_qualified_name(source)
                                if split_result:
                                    src_model, src_col = split_result
                                    src_node_id = f"col_{src_model}_{src_col}"
                                    edge = GraphEdge(
                                        source=src_node_id, target=main_node_id
                                    ).model_dump()
                                    if not any(
                                        e["source"] == edge["source"]
                                        and e["target"] == edge["target"]
                                        for e in self.data.edges
                                    ):
                                        self.data.edges.append(edge)
            except Exception as e:
                logger.debug(f"Could not create main_node upstream edges: {e}")

        if direction == "downstream" and self.lineage_service:
            try:
                if "exposures" not in refs or not isinstance(refs["exposures"], set):
                    return

                exposure_names = refs["exposures"]
                if not exposure_names:
                    return

                for exposure_name in sorted(exposure_names):
                    try:
                        if not self.lineage_service:
                            continue
                        exposure = self.lineage_service.registry.get_exposure(exposure_name)
                        if not exposure or not hasattr(exposure, "depends_on_models"):
                            continue

                        exposure_node_id = f"exposure_{exposure_name}"

                        for model_name in sorted(exposure.depends_on_models):
                            model_in_refs = model_name in refs and isinstance(
                                refs[model_name], dict
                            )
                            is_starting_model = (
                                hasattr(self, "_start_model") and model_name == self._start_model
                            )

                            if model_in_refs or is_starting_model:
                                try:
                                    model = self.lineage_service.registry.get_model(model_name)
                                    if not model or not hasattr(model, "columns"):
                                        continue

                                    for col_name in sorted(model.columns.keys()):
                                        if is_starting_model and col_name != self._start_column:
                                            continue

                                        col_node_id = f"col_{model_name}_{col_name}"
                                        if any(
                                            n["id"] == col_node_id for n in self.data.nodes
                                        ) and any(
                                            n["id"] == exposure_node_id for n in self.data.nodes
                                        ):
                                            edge = GraphEdge(
                                                source=col_node_id,
                                                target=exposure_node_id,
                                                type="exposure",
                                            ).model_dump()
                                            if not any(
                                                e["source"] == edge["source"]
                                                and e["target"] == edge["target"]
                                                for e in self.data.edges
                                            ):
                                                self.data.edges.append(edge)
                                except (ValueError, KeyError, AttributeError) as e:
                                    logger.debug(
                                        f"Failed to process exposure edge for {model_name} -> {exposure_name}: {e}"
                                    )
                                    continue
                                except Exception as e:
                                    logger.warning(
                                        f"Unexpected error processing exposure edge: {e}"
                                    )
                                    continue
                    except (ValueError, KeyError) as e:
                        logger.debug(f"Exposure {exposure_name} not found: {e}")
                        continue
                    except Exception as e:
                        logger.warning(f"Failed to process exposure {exposure_name}: {e}")
                        continue
            except Exception as e:
                logger.warning(f"Failed to add exposure edges: {e}", exc_info=True)

    def set_lineage_service(self, lineage_service: "LineageService") -> None:
        """Set the lineage service for the explore server."""
        self.lineage_service = lineage_service

    def _set_column_info(self, column: Column) -> None:
        """Set the main column info for display."""
        model_name = column.model_name

        self.data.column_info = ColumnInfo(
            name=column.name,
            model=model_name,
            type=column.data_type,
            description=column.description,
        )

        resource_type = self._get_model_resource_type(model_name)

        self._add_node(
            id=f"col_{model_name}_{column.name}",
            label=column.name,
            model=model_name,
            data_type=column.data_type,
            is_main=True,
            resource_type=resource_type,
        )

    def _get_model_resource_type(self, model_name: str) -> str | None:
        """Get resource type for a model."""
        try:
            if self.lineage_service:
                model_obj = self.lineage_service.registry.get_model(model_name)
                if model_obj:
                    return getattr(model_obj, "resource_type", None)
        except Exception:
            pass
        return None

    def start(self) -> None:
        """Start the server to display the graph."""
        uvicorn.run(self.app, host=self.host, port=self.port)

    def _add_node(
        self,
        id: str,
        label: str,
        model: str,
        data_type: str | None = None,
        is_main: bool = False,
        resource_type: str | None = None,
        is_key: bool = False,
    ) -> dict[str, Any]:
        """Helper to create and add a node."""
        node = GraphNode(
            id=id,
            label=label,
            type="column",
            model=model,
            data_type=data_type,
            is_main=is_main,
            resource_type=resource_type,
            is_key=is_key,
        ).model_dump()

        self.data.nodes.append(node)
        return node

    def _add_edge(self, source_id: str, target_id: str) -> dict[str, str]:
        """Helper to create and add an edge."""
        edge = GraphEdge(source=source_id, target=target_id, type="lineage").model_dump()

        self.data.edges.append(edge)
        return edge

    def _process_refs(
        self,
        refs: Mapping[str, dict[str, ColumnLineage] | set[str]],
        direction: str,
        main_node_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Process reference data into nodes and edges."""
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids = set()

        if "exposures" in refs and isinstance(refs["exposures"], set):
            exposure_names = refs["exposures"]
            for exposure_name in sorted(exposure_names):
                try:
                    if not self.lineage_service:
                        continue
                    exposure = self.lineage_service.registry.get_exposure(exposure_name)
                    exposure_node_id = f"exposure_{exposure_name}"
                    if exposure_node_id not in node_ids:
                        exposure_node = GraphNode(
                            id=exposure_node_id,
                            label=exposure_name,
                            type="exposure",
                            model=exposure_name,
                            resource_type="exposure",
                        ).model_dump()
                        exposure_node["exposure_data"] = {
                            "name": exposure.name,
                            "type": exposure.type,
                            "url": exposure.url,
                            "description": exposure.description,
                        }
                        nodes.append(exposure_node)
                        node_ids.add(exposure_node_id)
                except Exception as e:
                    logger.warning(f"Failed to process exposure {exposure_name}: {e}")

        for model_name, columns in sorted(refs.items()):
            if model_name == "exposures" or not isinstance(columns, dict):
                continue

            model_resource_type = self._get_model_resource_type(model_name)

            for col_name, lineage in sorted(columns.items()):
                col_node_id = f"col_{model_name}_{col_name}"
                if col_node_id not in node_ids:
                    col_node = GraphNode(
                        id=col_node_id,
                        label=col_name,
                        type="column",
                        model=model_name,
                        data_type=getattr(lineage, "data_type", None),
                        resource_type=model_resource_type,
                    ).model_dump()
                    col_node["direction"] = direction

                    nodes.append(col_node)
                    node_ids.add(col_node_id)

                if direction == "upstream" and hasattr(lineage, "source_columns"):
                    filtered_sources = [
                        src
                        for src in lineage.source_columns
                        if not src.lower().endswith(f"{model_name}.{col_name}".lower())
                    ]
                    if filtered_sources:
                        self._process_source_columns(
                            filtered_sources, col_node_id, refs, nodes, edges, node_ids
                        )
                elif direction == "downstream" and hasattr(lineage, "source_columns"):
                    self._add_downstream_edges(lineage.source_columns, col_node_id, edges)

                    if "exposures" in refs and isinstance(refs["exposures"], set):
                        for exposure_name in sorted(refs["exposures"]):
                            try:
                                if not self.lineage_service:
                                    continue
                                exposure = self.lineage_service.registry.get_exposure(exposure_name)
                                if model_name in exposure.depends_on_models:
                                    exposure_node_id = f"exposure_{exposure_name}"
                                    if exposure_node_id in node_ids:
                                        edge = GraphEdge(
                                            source=col_node_id,
                                            target=exposure_node_id,
                                            type="exposure",
                                        ).model_dump()
                                        if not any(
                                            e["source"] == edge["source"]
                                            and e["target"] == edge["target"]
                                            for e in edges
                                        ):
                                            edges.append(edge)
                            except Exception:
                                pass

        return {"nodes": nodes, "edges": edges}

    def _split_qualified_name(self, qualified_name: str) -> tuple[str, str] | None:
        """Split a fully qualified name into model and column parts. Returns None if invalid."""
        if "." not in qualified_name:
            return None
        parts = qualified_name.split(".")
        if len(parts) < 2:
            return None
        model_part = ".".join(parts[:-1])
        column_part = parts[-1]
        return (model_part, column_part)

    def _add_downstream_edges(
        self,
        source_columns: list[str] | set[str],
        target_node_id: str,
        edges: list[dict[str, Any]],
    ) -> None:
        """Add edges for downstream lineage."""
        for source in source_columns:
            split_result = self._split_qualified_name(source)
            if split_result is None:
                continue
            src_model, src_col = split_result
            src_node_id = f"col_{src_model}_{src_col}"
            edge = GraphEdge(source=src_node_id, target=target_node_id).model_dump()
            edges.append(edge)

    def _process_source_columns(
        self,
        source_columns: list[str] | set[str],
        target_node_id: str,
        refs: Mapping[str, dict[str, ColumnLineage] | set[str]],
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        node_ids: set[str],
    ) -> None:
        """Process source columns and create nodes/edges."""
        for source in source_columns:
            split_result = self._split_qualified_name(source)
            if split_result is None:
                continue
            src_model, src_col = split_result
            src_node_id = f"col_{src_model}_{src_col}"

            if src_node_id not in node_ids:
                self._add_source_node(src_model, src_col, refs, nodes, node_ids)

            edge = GraphEdge(source=src_node_id, target=target_node_id).model_dump()
            edges.append(edge)

    def _add_source_node(
        self,
        src_model: str,
        src_col: str,
        refs: Mapping[str, dict[str, ColumnLineage] | set[str]],
        nodes: list[dict[str, Any]],
        node_ids: set[str],
    ) -> None:
        """Add a source node to the graph."""
        src_node_id = f"col_{src_model}_{src_col}"
        src_data_type = None
        model_resource_type = self._get_model_resource_type(src_model)

        if src_model in refs and isinstance(refs[src_model], dict):
            src_model_data = refs[src_model]
            if isinstance(src_model_data, dict) and src_col in src_model_data:
                src_lineage = src_model_data[src_col]
                src_data_type = getattr(src_lineage, "data_type", None)

        src_node = GraphNode(
            id=src_node_id,
            label=src_col,
            type="column",
            model=src_model,
            data_type=src_data_type,
            resource_type=model_resource_type,
        ).model_dump()
        src_node["direction"] = "upstream"

        nodes.append(src_node)
        node_ids.add(src_node_id)
