from typing import Any

from graphviz import Digraph  # type: ignore  # missing stubs for graphviz

from parrant.lineage.display.base import LineageStaticDisplay
from parrant.lineage.provider import LineageProvider
from parrant.models.schema import Column, ColumnLineage


class DotDisplay(LineageStaticDisplay):
    def __init__(self, output_file: str = "lineage.dot", registry: LineageProvider | None = None):
        self.dot = Digraph(comment="Column Lineage")
        self.dot.attr(rankdir="LR")
        self.dot.attr("node", fontname="Helvetica")
        self.dot.attr("edge", fontname="Helvetica")
        self.dot.attr(nodesep="1.0")
        self.dot.attr(ranksep="1.0")
        self.output_file = output_file
        self.models: dict[str, Any] = {}
        self.registry = registry
        self.model_columns: dict[str, dict[str, str]] = {}
        self.edges: set[tuple[str, str]] = set()
        self.main_model: str = ""
        self.main_column: str = ""

    def display_column_info(self, column: Column) -> None:
        self._add_column_to_model(column.model_name, column.name, column.data_type)

    def _add_column_to_model(
        self, model_name: str, col_name: str, data_type: str | None = None
    ) -> None:
        if model_name not in self.model_columns:
            self.model_columns[model_name] = {}

        if not data_type and self.registry:
            model = self.registry.get_model(model_name)
            if col_name in model.columns:
                data_type = model.columns[col_name].data_type

        parts = [col_name]
        if data_type:
            parts.append(f"type: {data_type}")

        self.model_columns[model_name][col_name] = "\n".join(parts)

    def _create_model_subgraph(self, model_name: str, is_main: bool = False) -> None:
        if model_name not in self.models:
            with self.dot.subgraph(name=f"cluster_{model_name}") as cluster:
                cluster.attr(
                    label=model_name,
                    style="filled",
                    color="lightblue" if is_main else "lightgreen",
                    fontname="Helvetica Bold",
                )

                columns = self.model_columns.get(model_name, {})
                for col_name, label in columns.items():
                    cluster.node(
                        f"{model_name}.{col_name}",
                        label,
                        shape="box",
                        style="filled",
                        fillcolor="white",
                    )

                self.models[model_name] = cluster

    def _add_edge(self, from_ref: str, to_ref: str) -> None:
        edge = (from_ref, to_ref)
        if edge not in self.edges:
            self.dot.edge(from_ref, to_ref)
            self.edges.add(edge)

    def _process_model_chain(
        self,
        current_model_name: str,
        current_col_name: str,
        model_refs: dict[str, dict[str, ColumnLineage]],
        processed: set[str] | None = None,
    ) -> None:
        if self.registry is None:
            return

        if processed is None:
            processed = set()

        current_ref = f"{current_model_name}.{current_col_name}"
        if current_ref in processed:
            return
        processed.add(current_ref)

        for model_name, columns in model_refs.items():
            for col_name, lineage in columns.items():
                if any(src == current_ref for src in lineage.source_columns):
                    model = self.registry.get_model(model_name)
                    data_type = (
                        model.columns[col_name].data_type if col_name in model.columns else None
                    )

                    self._add_column_to_model(model_name, col_name, data_type=data_type)
                    self._add_edge(current_ref, f"{model_name}.{col_name}")
                    self._process_model_chain(model_name, col_name, model_refs, processed)

    def _add_refs(self, refs: dict[str, dict[str, ColumnLineage]], direction: str) -> None:
        if not refs:
            return

        model_refs = {k: v for k, v in refs.items() if k not in ("sources", "direct_refs")}

        if direction == "downstream":
            self._process_model_chain(self.main_model, self.main_column, model_refs)

        for model_name in self.model_columns:
            self._create_model_subgraph(model_name, model_name == self.main_model)

    def display_upstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        model_refs = {
            k: v
            for k, v in refs.items()
            if k not in ("sources", "direct_refs") and isinstance(v, dict)
        }
        self._add_refs(model_refs, direction="upstream")

    def display_downstream(self, refs: dict[str, dict[str, ColumnLineage] | set[str]]) -> None:
        model_refs = {
            k: v
            for k, v in refs.items()
            if k not in ("exposures", "sources", "direct_refs") and isinstance(v, dict)
        }
        self._add_refs(model_refs, direction="downstream")

    def save(self) -> None:
        self.dot.render(self.output_file, view=True, format="png")
