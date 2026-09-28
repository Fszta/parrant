import os
from pathlib import Path
from typing import Any

import duckdb
from dbt.cli.main import dbtRunner


def setup_test_db(project_dir: Path) -> Path:
    """Set up a test DuckDB database with sample data."""
    db_path = project_dir / "test.duckdb"
    if db_path.exists():
        db_path.unlink()

    conn = duckdb.connect(str(db_path))

    # Drop tables if they exist to ensure clean state
    conn.execute("DROP TABLE IF EXISTS raw_accounts")
    conn.execute("DROP TABLE IF EXISTS raw_countries")
    conn.execute("DROP TABLE IF EXISTS raw_transactions")

    conn.execute(
        """
    CREATE TABLE raw_accounts (
        id INTEGER PRIMARY KEY,
        holder TEXT,
        country_id INTEGER
    )
    """
    )

    conn.execute(
        """
    CREATE TABLE raw_countries (
        id INTEGER PRIMARY KEY,
        code TEXT,
        name TEXT
    )
    """
    )

    conn.execute(
        """
    CREATE TABLE raw_transactions (
        id INTEGER PRIMARY KEY,
        account_id INTEGER,
        amount REAL,
        status TEXT,
        transaction_date TEXT
    )
    """
    )

    for table in ["raw_accounts", "raw_countries", "raw_transactions"]:
        csv_path = project_dir / "raw_test_data" / f"{table}.csv"
        conn.execute(f"COPY {table} FROM '{csv_path}' (AUTO_DETECT TRUE)")

    conn.close()

    return db_path


def setup_dbt_project(project_dir: Path) -> dict[str, Any]:
    """Setup dbt project and return paths to artifacts."""
    dbt = dbtRunner()

    original_dir = os.getcwd()

    try:
        os.chdir(project_dir)
        os.environ["DBT_PROFILES_DIR"] = str(project_dir)

        db_path = setup_test_db(project_dir)

        conn = duckdb.connect(str(db_path))

        print("\nVerifying database tables:")
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
        for table in tables:
            print(f"- {table[0]}")
            count = conn.execute(f"SELECT COUNT(*) FROM {table[0]}").fetchone()
            if count:
                print(f"  Rows: {count[0]}")

        conn.close()

        commands = [["run"], ["snapshot"], ["docs", "generate"]]

        for cmd in commands:
            print(f"\nRunning dbt {' '.join(cmd)}")
            res = dbt.invoke(cmd)

            if not res.success:
                error_msg = f"dbt {' '.join(cmd)} failed"

                if hasattr(res, "exception") and res.exception:
                    error_msg += f": {res.exception}"

                if hasattr(res, "result") and res.result:
                    result_obj = res.result
                    errors = getattr(result_obj, "errors", None)
                    if errors:
                        error_msg += f"\nErrors: {errors}"
                    error = getattr(result_obj, "error", None)
                    if error:
                        error_msg += f"\nError: {error}"

                # If it's the docs generate command, try to get more info about the database
                if cmd[0] == "docs" and cmd[1] == "generate":
                    try:
                        conn = duckdb.connect(str(db_path))

                        # Check if the source tables exist
                        for source_table in [
                            "raw_accounts",
                            "raw_countries",
                            "raw_transactions",
                        ]:
                            result = conn.execute(
                                f"""
                                SELECT COUNT(*)
                                FROM information_schema.tables
                                WHERE table_schema='main' AND table_name='{source_table}'
                            """
                            ).fetchone()
                            exists = result[0] if result else 0
                            if exists:
                                print(f"Table {source_table} exists")
                            else:
                                print(f"Table {source_table} does NOT exist")

                        conn.close()
                    except Exception as e:
                        error_msg += f"\nDatabase inspection error: {e!s}"

                raise Exception(error_msg)

        return {
            "catalog_path": project_dir / "target" / "catalog.json",
            "manifest_path": project_dir / "target" / "manifest.json",
        }
    finally:
        os.chdir(original_dir)
