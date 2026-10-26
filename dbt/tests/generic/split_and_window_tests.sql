{#- V7 generic tests (docs/V3_DESIGN.md 10.4, edits 32, 39 and 45). -#}

{#- Edit 32. Every split adds back to its total: within each `total_by` group, the sum of
    `measure` over the split rows equals the row(s) marked as the total. Use on a mart that
    carries both detail and subtotal rows (GROUPING SETS); `is_total_expr` marks the total row.
    Fails per group whose split differs from its total by more than `tolerance`. -#}
{% test assert_split_sums_to_total(model, measure, is_total_expr, total_by=[], tolerance=0.01) %}
with g as (
    select
        {% for c in total_by %}{{ c }}, {% endfor %}
        sum(case when {{ is_total_expr }} then {{ measure }} else 0 end) as total_value,
        sum(case when {{ is_total_expr }} then 0 else {{ measure }} end) as split_value,
        count(*) filter (where {{ is_total_expr }}) as total_rows
    from {{ model }}
    {% if total_by %}group by {{ total_by | join(', ') }}{% endif %}
)
select *
from g
where total_rows > 0 and abs(total_value - split_value) > {{ tolerance }}
{% endtest %}

{#- Edit 32, the other shape: a breakdown model adds back to a separate total model.
    sum(`measure`) of this model by `join_by` = sum(`total_measure`) of `total_model` by `join_by`
    (`total_where` filters the total model; the model-level `where` config filters this one). -#}
{% test split_matches_total_model(model, measure, total_model, total_measure, join_by=[], tolerance=0.01, total_where=none) %}
with s as (
    select {% for c in join_by %}{{ c }}, {% endfor %} sum({{ measure }}) as split_value
    from {{ model }}
    {% if join_by %}group by {{ join_by | join(', ') }}{% endif %}
),
t as (
    select {% for c in join_by %}{{ c }}, {% endfor %} sum({{ total_measure }}) as total_value
    from {{ total_model }}
    {% if total_where %}where {{ total_where }}{% endif %}
    {% if join_by %}group by {{ join_by | join(', ') }}{% endif %}
)
select *
from s
full outer join t {% if join_by %}using ({{ join_by | join(', ') }}){% else %}on true{% endif %}
where abs(coalesce(split_value, 0) - coalesce(total_value, 0)) > {{ tolerance }}
{% endtest %}

{#- Edit 39. The same payment twice within `days` days: rows that share every `match_on` column
    (for example payee + amount) with an earlier row no more than `days` days before. -#}
{% test no_repeat_within_days(model, match_on, date_col, days=7, key_col=none) %}
with x as (
    select
        {{ match_on | join(', ') }},
        cast({{ date_col }} as date) as d,
        lag(cast({{ date_col }} as date)) over (
            partition by {{ match_on | join(', ') }}
            order by cast({{ date_col }} as date){% if key_col %}, {{ key_col }}{% endif %}
        ) as prev_d
    from {{ model }}
    where {% for c in match_on %}{{ c }} is not null{% if not loop.last %} and {% endif %}{% endfor %}
)
select *
from x
where prev_d is not null and datediff('day', prev_d, d) <= {{ days }}
{% endtest %}

{#- Edit 45. Every id carries its tenant: (`tenant_col`, column) is unique. The id alone may repeat
    across tenants (a reused id is two companies, never one), so never join on the id alone. -#}
{% test unique_per_tenant(model, column_name, tenant_col='tenant_key') %}
select {{ tenant_col }}, {{ column_name }}, count(*) as n
from {{ model }}
where {{ column_name }} is not null
group by {{ tenant_col }}, {{ column_name }}
having count(*) > 1
{% endtest %}
