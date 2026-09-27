import json
from pathlib import Path
from typing import Any

from parrant.models.schema import Model


class CatalogReader:
    def __init__(self, catalog_path: str):
        self.catalog_path = Path(catalog_path)
        self.catalog: dict[str, Any] = {}

    def load(self) -> None:
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"Catalog file not found: {self.catalog_path}")
        with open(self.catalog_path, "r") as f:
            self.catalog = json.load(f)

    def get_models_nodes(self) -> dict[str, Model]:
        models = {}
        nodes = self.catalog.get("nodes", {})
        sources = self.catalog.get("sources", {})

        for node_id, model_data in nodes.items():
            resource_type = node_id.split(".")[0]
            # In dbt's catalog.json the schema/database/name live under `metadata`,
            # not at the top level of the node.
            metadata = model_data.get("metadata", {})
            model_name = (
                metadata.get("name") or model_data.get("name") or node_id.split(".")[-1]
            ).lower()
            processed_data = {
                "name": model_name,
                "schema": metadata.get("schema") or model_data.get("schema") or "main",
                "database": metadata.get("database") or model_data.get("database") or "main",
                "columns": {},
                "resource_type": resource_type,
            }

            for col_name, col_data in model_data.get("columns", {}).items():
                normalized_col_name = col_name.lower()
                processed_data["columns"][normalized_col_name] = {
                    "name": normalized_col_name,
                    "model_name": model_name,
                    "description": col_data.get("description"),
                    "data_type": col_data.get("type") or col_data.get("data_type"),
                    "lineage": [],
                }

            model = Model(**processed_data)
            models[model.name] = model

        for source_id, source_data in sources.items():
            metadata = source_data.get("metadata", {})
            source_identifier = metadata.get("name")
            normalized_source_identifier = source_identifier.lower() if source_identifier else None

            table_name = (source_data.get("name") or source_id.split(".")[-1]).lower()
            source_name = source_data.get("source_name")
            if not source_name:
                source_id_parts = source_id.split(".")
                if len(source_id_parts) >= 4:
                    source_name = source_id_parts[2]
            normalized_source_name = source_name.lower() if source_name else None

            processed_data = {
                "name": table_name,
                "schema": metadata.get("schema") or source_data.get("schema") or "main",
                "database": metadata.get("database") or source_data.get("database") or "main",
                "columns": {},
                "resource_type": "source",
                "source_identifier": normalized_source_identifier,
                "source_name": normalized_source_name,
            }

            for col_name, col_data in source_data.get("columns", {}).items():
                normalized_col_name = col_name.lower()
                processed_data["columns"][normalized_col_name] = {
                    "name": normalized_col_name,
                    "model_name": normalized_source_identifier or table_name,
                    "description": col_data.get("description"),
                    "data_type": col_data.get("type") or col_data.get("data_type"),
                    "lineage": [],
                }

            model = Model(**processed_data)
            key = normalized_source_identifier or model.name
            models[key] = model
            for col in model.columns.values():
                col.model_name = key

        return models
