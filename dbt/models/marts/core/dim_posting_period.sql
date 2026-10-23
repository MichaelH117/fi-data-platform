-- Accounting periods. Every financial fact's posting_period_key is an FK to this table.
-- is_closed: the period closed (business day N of the next month) on or before the as-of date.
select
    posting_period_key,
    period_start,
    period_end,
    close_date,
    is_closed,
    fiscal_year,
    fiscal_quarter,
    fiscal_month,
    period_label,
    {{ audit_columns() }}
from {{ ref('int_posting_periods') }}
