{#- Posting-period rule (docs/V3_DESIGN.md section 2.5), used by every financial fact.

    posting_period_key = greatest(month of the event date,
                                  first period still open on the day the event was recorded)

    So an event posts to its own month while that month is open. Once the month is closed
    (end of business day N of the next month, see int_posting_periods), a late event posts to
    the first open period instead and keeps its true event date in its *_date_key column.

    Usage:
        select {{ posting_period_key('t.posted_at', 'pp') }} as posting_period_key
        from ... as t
        {{ posting_period_join('t.created_at', 'pp') }}
-#}

{% macro posting_period_join(recorded_col, alias='pp') -%}
    left join {{ ref('int_posting_days') }} as {{ alias }}
        on {{ alias }}.date_day = cast({{ recorded_col }} as date)
{%- endmacro %}

{% macro posting_period_key(event_col, alias='pp') -%}
    greatest({{ yyyymm(event_col) }}, coalesce({{ alias }}.first_open_period_key, {{ yyyymm(event_col) }}))
{%- endmacro %}
