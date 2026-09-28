from parrant.artifacts.manifest import ManifestReader


def test_get_model_dependencies_does_not_raise(dbt_artifacts):
    """Regression: get_model_dependencies() reads real dbt artifacts without a TypeError.

    `depends_on.nodes` is a list of unique_id strings (e.g. "model.pkg.name"), but the
    method used to treat each entry as a dict via dep['alias'], raising TypeError on call.
    """
    reader = ManifestReader(str(dbt_artifacts["manifest_path"]))
    reader.load()

    # Before the fix this raised TypeError: string indices must be integers.
    dependencies = reader.get_model_dependencies()

    assert isinstance(dependencies, dict)
    assert dependencies, "expected at least one model with dependencies"

    transactions_id = next(
        node_id
        for node_id in dependencies
        if node_id.endswith(".transactions") and node_id.startswith("model.")
    )
    deps = dependencies[transactions_id]
    assert isinstance(deps, set)
    # transactions is built from the staging models; entries are full unique_ids.
    assert any(dep.endswith(".stg_transactions") for dep in deps)
    assert all(isinstance(dep, str) and "." in dep for dep in deps)
