{#- Row-level assertion: fails for every row where the expression is false. -#}
{% test expression_is_true(model, expression, where_clause=none, column_name=none) %}
select *
from {{ model }}
where not ({{ expression }})
{% if where_clause %}and ({{ where_clause }}){% endif %}
{% endtest %}
