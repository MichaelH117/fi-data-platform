-- One row: the reporting as-of date and the periods around it, so consumers (reports, publish,
-- semantic) never read intermediate models to know "today".
select
    a.as_of_date,
    {{ date_key('a.as_of_date') }} as as_of_date_key,
    a.cutover_date,
    {{ yyyymm('a.as_of_date') }} as current_period_key,
    (select max(posting_period_key) from {{ ref('int_posting_periods') }} where is_closed) as last_closed_period_key,
    cast(date_trunc('month', a.as_of_date) - interval 1 month as date) as last_complete_month,
    {{ audit_columns() }}
from {{ ref('int_as_of_date') }} as a
