-- State market survey: numbered regulator items by provider x calendar year
-- (grain: state x provider x calendar_year x item_no). Only states with market_survey = true.
-- The current year is year-to-date and preliminary (is_final = false). The report adds a Total
-- column using `aggregation`: SUM for flows, LAST (latest year) for the in-force count.
select
    state,
    provider_key,
    provider_name,
    cast(left(window_id, 4) as integer) as calendar_year,
    window_label as year_label,
    window_start as year_start,
    window_end as year_end,
    is_final,
    line_no as item_no,
    line_item as item,
    label as item_label,
    form_question_no,
    basis,
    aggregation,
    value,
    {{ audit_columns() }}
from {{ ref('mart_regulatory_line_values') }}
where report_id = 'MARKET_SURVEY'
