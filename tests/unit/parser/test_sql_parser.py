from parrant.parser import SQLColumnParser


def test_simple_select_with_join():
    """Test parsing a simple SELECT statement with direct references and renames."""
    sql = """
    select
        customers.id as customer_id,
        customers.name,
        orders.amount
    from customers
    join orders on orders.customer_id = customers.id
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert set(lineage.keys()) == {"customer_id", "name", "amount"}
    assert lineage["customer_id"][0].transformation_type == "renamed"
    assert lineage["customer_id"][0].source_columns == {"customers.id"}
    assert lineage["name"][0].transformation_type == "direct"
    assert lineage["name"][0].source_columns == {"customers.name"}
    assert lineage["amount"][0].transformation_type == "direct"
    assert lineage["amount"][0].source_columns == {"orders.amount"}


def test_cte_with_aggregation_and_table_aliases():
    """Test parsing a query with CTEs and aggregations."""
    sql = """
    with customer_orders as (
        select
            customer_id,
            count(orders.id) as order_count,
            sum(orders.amount) as total_amount
        from orders
        group by customer_id
    )
    select
        c.id,
        c.name,
        co.order_count,
        co.total_amount,
        case
            when co.total_amount > 1000 then 'high'
            else 'low'
        end as customer_tier
    from customers c
    left join customer_orders co on co.customer_id = c.id
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert lineage["id"][0].source_columns == {"customers.id"}
    assert lineage["name"][0].source_columns == {"customers.name"}
    assert lineage["order_count"][0].transformation_type == "derived"
    assert "count" in lineage["order_count"][0].sql_expression.lower()
    assert lineage["order_count"][0].source_columns == {"orders.id"}
    assert lineage["total_amount"][0].transformation_type == "derived"
    assert "sum" in lineage["total_amount"][0].sql_expression.lower()
    assert lineage["total_amount"][0].source_columns == {"orders.amount"}
    assert lineage["customer_tier"][0].transformation_type == "derived"
    assert "case" in lineage["customer_tier"][0].sql_expression.lower()
    assert lineage["customer_tier"][0].source_columns == {"orders.amount"}


def test_multiple_ctes():
    """Test parsing a query with multiple CTEs and wildcard select."""
    sql = """
    with orders as (
        select * from orders
    ),
    customers as (
        select * from customers
    ),
    customer_orders as (
        select
            customer_id,
            count(id) as order_count,
            sum(amount) as total_amount
        from orders
        group by customer_id
    ),
    customer_orders_with_tiering as (
        select
            *,
            case
                when total_amount > 1000 then 'tier_1'
                else 'tier_2'
            end as customer_tier
        from customer_orders
        left join customers on customers.id = customer_id
    ),
    final as (
        select * from customer_orders_with_tiering
    )
    select * from final
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert lineage["order_count"][0].transformation_type == "derived"
    assert lineage["order_count"][0].source_columns == {"orders.id"}
    assert lineage["total_amount"][0].transformation_type == "derived"
    assert lineage["total_amount"][0].source_columns == {"orders.amount"}
    assert lineage["customer_tier"][0].transformation_type == "derived"
    assert "total_amount" in lineage["customer_tier"][0].sql_expression
    assert lineage["customer_tier"][0].source_columns == {"orders.amount"}
    assert lineage["customer_id"][0].source_columns == {"orders.customer_id"}


def test_nested_ctes_with_transformations():
    """Test nested CTEs with multiple transformations of the same column."""
    sql = """
    with revenue as (
        select
            customer_id,
            sum(amount) as total_revenue
        from orders
        group by customer_id
    ),
    revenue_tiers as (
        select
            customer_id,
            total_revenue,
            case
                when total_revenue > 1000 then 'high'
                when total_revenue > 500 then 'medium'
                else 'low'
            end as revenue_tier,
            total_revenue / 100 as revenue_hundreds
        from revenue
    )
    select * from revenue_tiers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert lineage["total_revenue"][0].source_columns == {"orders.amount"}
    assert lineage["revenue_hundreds"][0].source_columns == {"orders.amount"}
    assert lineage["revenue_tier"][0].source_columns == {"orders.amount"}


def test_window_functions():
    """Test parsing window functions and partitions."""
    sql = """
    select
        customer_id,
        amount,
        sum(amount) over (partition by customer_id) as customer_total,
        rank() over (partition by customer_id order by amount desc) as amount_rank,
        amount / sum(amount) over (partition by customer_id) as amount_pct
    from orders
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert lineage["customer_total"][0].transformation_type == "derived"
    assert lineage["customer_total"][0].source_columns == {
        "orders.amount",
        "orders.customer_id",
    }
    assert lineage["amount_rank"][0].transformation_type == "derived"
    assert lineage["amount_rank"][0].source_columns == {
        "orders.amount",
        "orders.customer_id",
    }
    assert lineage["amount_pct"][0].transformation_type == "derived"
    assert lineage["amount_pct"][0].source_columns == {
        "orders.amount",
        "orders.customer_id",
    }


def test_subqueries():
    """Test parsing subqueries in different contexts."""
    sql = """
    select
        customers.id,
        customers.name,
        (select count(*) from orders where orders.customer_id = customers.id) as order_count,
        exists(
            select 1
            from orders
            where orders.customer_id = customers.id
            and orders.amount > 1000
        ) as has_large_orders
    from customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert lineage["order_count"][0].transformation_type == "derived"
    assert "orders.customer_id" in lineage["order_count"][0].source_columns
    assert "customers.id" in lineage["order_count"][0].source_columns
    assert lineage["has_large_orders"][0].transformation_type == "derived"
    assert "orders.amount" in lineage["has_large_orders"][0].source_columns
    assert "orders.customer_id" in lineage["has_large_orders"][0].source_columns
    assert "customers.id" in lineage["has_large_orders"][0].source_columns


def test_star_sources_should_not_include_ctes():
    """Test that star sources only include actual table references, not CTEs."""
    parser = SQLColumnParser()

    sql = """
    with source as (
        select * from raw_transactions
    ),
    final as (
        select * from source
    )
    select * from final
    """

    result = parser.parse_column_lineage(sql)

    assert result.star_sources == {"raw_transactions"}


def test_star_sources_with_multiple_base_tables():
    """Test star sources with multiple base tables through CTEs."""
    parser = SQLColumnParser()

    sql = """
    with transactions as (
        select * from raw_transactions
    ),
    accounts as (
        select * from raw_accounts
    ),
    enriched as (
        select t.*, a.*
        from transactions t
        join accounts a on a.id = t.account_id
    )
    select * from enriched
    """

    result = parser.parse_column_lineage(sql)
    assert result.star_sources == {"raw_transactions", "raw_accounts"}


def test_union_all_simple():
    """Test simple UNION ALL with two SELECT statements."""
    sql = """
    with source1 as (
        select id, name from table1
    ),
    source2 as (
        select id, name from table2
    ),
    final as (
        select id, name from source1
        union all
        select id, name from source2
    )
    select * from final
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "name" in lineage
    id_sources = {src for lineage_item in lineage["id"] for src in lineage_item.source_columns}
    name_sources = {src for lineage_item in lineage["name"] for src in lineage_item.source_columns}
    # BOTH branches must be traced — not just the left-most one.
    assert id_sources == {"table1.id", "table2.id"}
    assert name_sources == {"table1.name", "table2.name"}


def test_union_all_multiple_branches():
    """Test UNION ALL with multiple branches (4+ SELECT statements)."""
    sql = """
    with item1 as (select col1, col2 from source1),
    item2 as (select col1, col2 from source2),
    item3 as (select col1, col2 from source3),
    item4 as (select col1, col2 from source4),
    final as (
        select col1, col2 from item1
        union all
        select col1, col2 from item2
        union all
        select col1, col2 from item3
        union all
        select col1, col2 from item4
    )
    select * from final
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "col1" in lineage
    assert "col2" in lineage
    col1_sources = {src for lineage_item in lineage["col1"] for src in lineage_item.source_columns}
    col2_sources = {src for lineage_item in lineage["col2"] for src in lineage_item.source_columns}
    # Every one of the four branches must contribute its source.
    assert col1_sources == {"source1.col1", "source2.col1", "source3.col1", "source4.col1"}
    assert col2_sources == {"source1.col2", "source2.col2", "source3.col2", "source4.col2"}


def test_union_all_top_level_two_branches():
    """A top-level UNION ALL (no wrapping CTE) must trace both branches' sources."""
    sql = """
    select customer_id, amount from source_a
    union all
    select customer_id, amount from source_b
    """
    result = SQLColumnParser().parse_column_lineage(sql)
    lineage = result.column_lineage

    cust_sources = {src for item in lineage["customer_id"] for src in item.source_columns}
    amount_sources = {src for item in lineage["amount"] for src in item.source_columns}
    assert cust_sources == {"source_a.customer_id", "source_b.customer_id"}
    assert amount_sources == {"source_a.amount", "source_b.amount"}


def test_union_all_three_way_top_level():
    """A chained 3-way top-level UNION ALL must trace all three branches."""
    sql = """
    select id from t1
    union all
    select id from t2
    union all
    select id from t3
    """
    result = SQLColumnParser().parse_column_lineage(sql)
    id_sources = {src for item in result.column_lineage["id"] for src in item.source_columns}
    assert id_sources == {"t1.id", "t2.id", "t3.id"}


def test_union_inside_cte_referenced_by_name():
    """A union CTE referenced by explicit column names carries every branch's source."""
    sql = """
    with a as (select id, name from t1),
    b as (select id, name from t2),
    u as (
        select id, name from a
        union all
        select id, name from b
    )
    select id, name from u
    """
    result = SQLColumnParser().parse_column_lineage(sql)
    lineage = result.column_lineage
    id_sources = {src for item in lineage["id"] for src in item.source_columns}
    name_sources = {src for item in lineage["name"] for src in item.source_columns}
    assert id_sources == {"t1.id", "t2.id"}
    assert name_sources == {"t1.name", "t2.name"}


def test_exclude_single_column():
    """Test SELECT * EXCLUDE (column)."""
    sql = """
    with source as (
        select id, name, email, phone from users
    ),
    filtered as (
        select * exclude (email) from source
    )
    select * from filtered
    """
    parser = SQLColumnParser(dialect="snowflake")
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "name" in lineage
    assert "phone" in lineage
    assert "email" not in lineage


def test_exclude_multiple_columns():
    """Test SELECT * EXCLUDE (col1, col2)."""
    sql = """
    with source as (
        select a, b, c, d, e from table1
    ),
    filtered as (
        select * exclude (b, d) from source
    )
    select * from filtered
    """
    parser = SQLColumnParser(dialect="snowflake")
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "a" in lineage
    assert "c" in lineage
    assert "e" in lineage
    assert "b" not in lineage
    assert "d" not in lineage


def test_exclude_with_additional_columns():
    """Test EXCLUDE with additional transformed columns."""
    sql = """
    with source as (
        select id, name, email, phone from users
    ),
    transformed as (
        select
            * exclude (email),
            upper(name) as name_upper
        from source
    )
    select * from transformed
    """
    parser = SQLColumnParser(dialect="snowflake")
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "name" in lineage
    assert "phone" in lineage
    assert "name_upper" in lineage
    assert "email" not in lineage
    # name_upper should trace to users.name
    name_upper_sources = {
        src for lineage_item in lineage["name_upper"] for src in lineage_item.source_columns
    }
    assert any("name" in src.lower() for src in name_upper_sources)


def test_double_nested_subquery():
    """Test select * from (select * from ...)."""
    sql = """
    with source as (
        select * from (
            select * from raw_table limit 100
        )
    )
    select * from source
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)

    assert result.star_sources == {"raw_table"}


def test_triple_nested_subquery():
    """Test triple nested subqueries."""
    sql = """
    with source as (
        select * from (
            select * from (
                select id, name from base_table limit 100
            ) limit 100
        )
    )
    select id, name from source
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "name" in lineage
    id_sources = {src for lineage_item in lineage["id"] for src in lineage_item.source_columns}
    assert any("base_table" in src for src in id_sources)


def test_cte_with_multiple_dependencies():
    """Test CTE that depends on multiple other CTEs."""
    sql = """
    with cte1 as (
        select id, name from table1
    ),
    cte2 as (
        select id, value from table2
    ),
    combined as (
        select
            cte1.*,
            cte2.value
        from cte1
        join cte2 on cte1.id = cte2.id
    )
    select * from combined
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "name" in lineage
    assert "value" in lineage

    # id and name should trace to table1
    name_sources = {src for lineage_item in lineage["name"] for src in lineage_item.source_columns}
    assert any("table1" in src for src in name_sources)

    # value should trace to table2
    value_sources = {
        src for lineage_item in lineage["value"] for src in lineage_item.source_columns
    }
    assert any("table2" in src for src in value_sources)


def test_cte_chain_with_transformations():
    """Test chain of CTEs with transformations at each level."""
    sql = """
    with base as (
        select id, amount from transactions
    ),
    aggregated as (
        select
            id,
            sum(amount) as total_amount
        from base
        group by id
    ),
    enriched as (
        select
            id,
            total_amount,
            case
                when total_amount > 1000 then 'high'
                else 'low'
            end as tier
        from aggregated
    )
    select * from enriched
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "id" in lineage
    assert "total_amount" in lineage
    assert "tier" in lineage

    # total_amount should trace to transactions.amount
    total_sources = {
        src for lineage_item in lineage["total_amount"] for src in lineage_item.source_columns
    }
    assert any("amount" in src.lower() for src in total_sources)

    # tier should also trace to transactions.amount (through total_amount)
    tier_sources = {src for lineage_item in lineage["tier"] for src in lineage_item.source_columns}
    assert any("amount" in src.lower() for src in tier_sources)


def test_window_function_with_qualify():
    """Test QUALIFY clause with window functions."""
    sql = """
    with source as (
        select
            account_id,
            date,
            balance
        from account_balances
    ),
    latest as (
        select
            account_id,
            date,
            balance
        from source
        qualify row_number() over (partition by account_id order by date desc) = 1
    )
    select * from latest
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "account_id" in lineage
    assert "date" in lineage
    assert "balance" in lineage

    account_sources = {
        src for lineage_item in lineage["account_id"] for src in lineage_item.source_columns
    }
    assert any("account_balances" in src for src in account_sources)


def test_window_function_aggregation():
    """Test window functions with aggregations."""
    sql = """
    with source as (
        select
            account_id,
            date,
            balance
        from account_balances
    ),
    with_avg as (
        select
            account_id,
            date,
            balance,
            avg(balance) over (partition by account_id) as avg_balance
        from source
    )
    select * from with_avg
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "avg_balance" in lineage
    # avg_balance should trace to account_balances.balance
    avg_sources = {
        src for lineage_item in lineage["avg_balance"] for src in lineage_item.source_columns
    }
    assert any("balance" in src.lower() for src in avg_sources)


def test_case_with_forward_reference():
    """Test CASE statement referencing a column defined earlier."""
    sql = """
    with source as (
        select
            country_code,
            case
                when country_code = 'ITA' then '1'
                else '2'
            end as sub_item_00004,
            case
                when sub_item_00004 = '1' then 'value1'
                else 'value2'
            end as sub_item_00015
        from countries
    )
    select * from source
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "sub_item_00004" in lineage
    assert "sub_item_00015" in lineage

    # sub_item_00015 should trace to countries.country_code (through sub_item_00004)
    sub_item_sources = {
        src for lineage_item in lineage["sub_item_00015"] for src in lineage_item.source_columns
    }
    assert any("country_code" in src.lower() for src in sub_item_sources)


def test_join_with_date_arithmetic():
    """Test join with date arithmetic in ON clause."""
    sql = """
    with dates as (
        select last_day(date_day, 'quarter') as last_quarter_day
        from all_days
    ),
    accounts as (
        select
            account_id,
            account_upgraded_at,
            account_closed_at
        from account_holders
    ),
    cross_joined as (
        select *
        from dates
        inner join accounts
            on accounts.account_upgraded_at < dates.last_quarter_day + interval '1 day'
            and accounts.account_closed_at >= dates.last_quarter_day + interval '1 day'
    )
    select * from cross_joined
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "last_quarter_day" in lineage
    assert "account_id" in lineage
    account_sources = {
        src for lineage_item in lineage["account_id"] for src in lineage_item.source_columns
    }
    assert any("account_holders" in src for src in account_sources)


def test_fully_qualified_table_name():
    """Test parsing with fully qualified table names."""
    sql = """
    with source as (
        select * from ANALYTICS_DEV.dbt_schema.stg_table limit 100
    )
    select * from source
    """
    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)

    assert result.star_sources == {"stg_table"} or "stg_table" in str(result.star_sources)


def test_complex_query_structure():
    """Test a simplified version of the complex query structure."""
    sql = """
    with
    italian_mapping_country as (
        select * from (select * from stg_seeds__italian_report_country_mapping limit 100)
    ),
    accounts as (
        select * from (select * from stg_account_contract__accounts limit 100)
    ),
    payment_member as (
        select
            * exclude (payment_member_tax_identification_number),
            upper(trim(payment_member_tax_identification_number)) as payment_member_tax_identification_number
        from (select * from stg_account_contract__payment_members limit 100)
    ),
    account_memberships_with_tax as (
        select
            account_memberships.*,
            payment_member.payment_member_tax_identification_number
        from account_memberships
        left join payment_member on account_memberships.payment_member_id = payment_member.payment_member_id
    ),
    card_events as (
        select
            account_holder_16char_id,
            account_holder_id,
            account_number,
            last_quarter_day,
            'cardId_lastQuarterDay' as event_type,
            card_id || '_' || last_quarter_day as event_id,
            case
                when account_holder_country_cca3 = 'ITA' then '1'
                else '2'
            end as sub_item_00004,
            case
                when sub_item_00004 = '1' then 'value1'
                else '00000'
            end as sub_item_00015
        from cards_scope_cross_quarters
    ),
    account_events as (
        select
            account_holder_16char_id,
            account_holder_id,
            account_number,
            last_quarter_day,
            'accountId_lastQuarterDay' as event_type,
            account_id || '_' || last_quarter_day as event_id
        from account_holders_scope_cross_quarters
    ),
    final as (
        select
            account_holder_16char_id,
            account_holder_id,
            account_number,
            event_id,
            event_type,
            event_date,
            item_id,
            item_type
        from card_events
        union all
        select
            account_holder_16char_id,
            account_holder_id,
            account_number,
            event_id,
            event_type,
            event_date,
            item_id,
            item_type
        from account_events
    )
    select * from final
    """
    parser = SQLColumnParser(dialect="snowflake")
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "account_holder_16char_id" in lineage
    assert "account_holder_id" in lineage
    assert "account_number" in lineage
    assert "event_id" in lineage
    assert "event_type" in lineage

    # event_id should trace back to source columns (card_id, last_quarter_day, etc.)
    event_id_sources = {
        src for lineage_item in lineage["event_id"] for src in lineage_item.source_columns
    }
    assert len(event_id_sources) > 0


def test_table_names_normalized_from_sql() -> None:
    """Test that table names extracted from SQL are normalized to lowercase."""
    from sqlglot import exp, parse_one

    from parrant.parser.sql_parser_utils import (
        get_all_tables_from_select,
        get_table_context,
    )

    # Test with uppercase table names (Snowflake style)
    sql = "SELECT * FROM RAW_ORDERS_TABLE"
    parsed = parse_one(sql, dialect="snowflake")
    select = parsed.find(exp.Select)

    table_context = get_table_context(select)
    assert table_context == "raw_orders_table"
    assert table_context != "RAW_ORDERS_TABLE"

    # Test with mixed case
    sql2 = "SELECT * FROM RawOrdersTable"
    parsed2 = parse_one(sql2, dialect="snowflake")
    select2 = parsed2.find(exp.Select)

    table_context2 = get_table_context(select2)
    assert table_context2 == "raworderstable"

    # Test get_all_tables_from_select
    sql3 = """
    SELECT * FROM RAW_ORDERS_TABLE o
    JOIN RAW_CUSTOMERS_TABLE c ON c.id = o.customer_id
    """
    parsed3 = parse_one(sql3, dialect="snowflake")
    select3 = parsed3.find(exp.Select)

    tables = get_all_tables_from_select(select3)
    assert "raw_orders_table" in tables
    assert "raw_customers_table" in tables
    assert "RAW_ORDERS_TABLE" not in tables


def test_strip_sql_comments_utility() -> None:
    """Test the strip_sql_comments utility function."""
    from parrant.parser.sql_parser_utils import strip_sql_comments

    # Test /* ... */ style comments
    assert strip_sql_comments("customer_name /* customer data */") == "customer_name"
    assert strip_sql_comments("col /* comment */ name") == "col name"
    assert strip_sql_comments("table.col /* comment */") == "table.col"

    # Test -- style comments
    assert strip_sql_comments("customer_name -- comment") == "customer_name"
    assert strip_sql_comments("col -- inline comment") == "col"

    # Test multiline comments
    assert strip_sql_comments("col /* multi\nline\ncomment */") == "col"

    # Test multiple comments
    assert strip_sql_comments("col /* first */ /* second */") == "col"
    assert strip_sql_comments("col /* comment */ -- another") == "col"

    # Test edge cases
    assert strip_sql_comments("") == ""
    assert strip_sql_comments("   ") == ""
    assert strip_sql_comments("no_comments") == "no_comments"
    assert strip_sql_comments("/* only comment */") == ""


def test_column_with_block_comment() -> None:
    """Test parsing SQL with /* ... */ comments in column names."""
    sql = """
    select
        customer_name /* customer data */,
        order_id /* order reference */
    from customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    # Column names should be clean (no comments)
    assert "customer_name" in lineage
    assert "order_id" in lineage
    assert "customer_name /* customer data */" not in lineage
    assert "order_id /* order reference */" not in lineage

    # Source columns should also be clean
    customer_sources = {
        src for lineage_item in lineage["customer_name"] for src in lineage_item.source_columns
    }
    assert all("/*" not in src and "*/" not in src for src in customer_sources)


def test_column_with_line_comment() -> None:
    """Test parsing SQL with -- comments in column names."""
    sql = """
    select
        customer_name, -- customer identifier
        order_id -- order reference
    from customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer_name" in lineage
    assert "order_id" in lineage
    assert "--" not in str(lineage["customer_name"][0].source_columns)


def test_qualified_column_with_comment() -> None:
    """Test parsing qualified column names with comments."""
    sql = """
    select
        customers.customer_name /* customer data */,
        orders.order_id
    from customers
    join orders on customers.id = orders.customer_id
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer_name" in lineage
    assert "order_id" in lineage

    # Source columns should be clean
    customer_sources = {
        src for lineage_item in lineage["customer_name"] for src in lineage_item.source_columns
    }
    assert all("/*" not in src and "*/" not in src for src in customer_sources)


def test_cte_with_comments() -> None:
    """Test parsing CTEs with comments in column references."""
    sql = """
    with customer_summary as (
        select
            customer_name /* customer identifier */,
            order_id
        from customers
    )
    select
        customer_name,
        order_id
    from customer_summary
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer_name" in lineage
    assert "order_id" in lineage

    # Source columns should trace back correctly without comments
    customer_sources = {
        src for lineage_item in lineage["customer_name"] for src in lineage_item.source_columns
    }
    assert all("/*" not in src and "*/" not in src for src in customer_sources)


def test_comments_in_join_condition() -> None:
    """Test parsing joins with comments in column references."""
    sql = """
    select
        c.customer_name /* customer data */,
        o.order_id
    from customers c
    join orders o on c.id /* join key */ = o.customer_id
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer_name" in lineage
    assert "order_id" in lineage

    # All source columns should be clean
    for lineage_list in lineage.values():
        for lineage_item in lineage_list:
            for src in lineage_item.source_columns:
                assert "/*" not in src and "*/" not in src
                assert "--" not in src or src.index("--") == len(src) - 2  # Only at end


def test_comments_in_aliased_columns() -> None:
    """Test parsing aliased columns with comments."""
    sql = """
    select
        customer_name /* customer identifier */ as customer,
        order_id -- order reference
    from customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer" in lineage
    assert "order_id" in lineage

    # Source columns should be clean
    customer_sources = {
        src for lineage_item in lineage["customer"] for src in lineage_item.source_columns
    }
    assert all("/*" not in src and "*/" not in src for src in customer_sources)


def test_comments_in_star_exclude() -> None:
    """Test parsing SELECT * EXCLUDE with comments."""
    sql = """
    select * exclude (
        customer_name /* customer identifier */,
        order_id -- order reference
    )
    from customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    # Excluded columns should not appear in lineage
    assert "customer_name" not in lineage
    assert "order_id" not in lineage


def test_comments_in_complex_query() -> None:
    """Test parsing a complex query with comments in multiple places."""
    sql = """
    with customer_data as (
        select
            customer_name /* customer identifier */,
            order_id -- order reference
        from customers /* source table */
    ),
    filtered_customers as (
        select
            customer_name,
            order_id
        from customer_data
        where customer_name is not null /* filter */
    )
    select
        customer_name /* final */,
        order_id
    from filtered_customers
    """

    parser = SQLColumnParser()
    result = parser.parse_column_lineage(sql)
    lineage = result.column_lineage

    assert "customer_name" in lineage
    assert "order_id" in lineage

    # Verify all source columns are clean
    for lineage_list in lineage.values():
        for lineage_item in lineage_list:
            for src in lineage_item.source_columns:
                assert "/*" not in src and "*/" not in src
