{#- V7 edit 22: the display rules every page follows (docs/REPORTS.md "Rules every page follows").
    One macro per rule, so marts, reports and Power BI format strings agree:
      1. a ratio with no denominator is NULL (shown blank, never 0);
      2. the current calendar month is flagged "in progress";
      3. a book younger than young_book_months shows a "young book" note;
      4. claims money has one sign convention: stored positive, shown negative in parentheses.
    The site helpers and the PBI format string "#,##0.00;(#,##0.00)" apply rule 4 from the column
    claims_display_amount, so no page flips a sign on its own. -#}

{#- Rule 1. Ratio with no (or zero) denominator -> NULL. Same as safe_divide, named for the rule. -#}
{% macro ratio_or_blank(num, den) -%}
    {{ safe_divide(num, den) }}
{%- endmacro %}

{#- Rule 2. True for the month that contains the reporting date (the month is not finished). -#}
{% macro is_in_progress_month(month_col, as_of_col=none) -%}
    {%- set as_of_col = as_of_col or "(select as_of_date from " ~ ref('int_as_of_date') ~ ")" -%}
    (date_trunc('month', cast({{ month_col }} as date)) = date_trunc('month', cast({{ as_of_col }} as date)))
{%- endmacro %}

{#- Rule 3. True when the book (first sale) is younger than var young_book_months (default 12)
    at the reporting date: its ratios are immature and the page shows the "young book" note. -#}
{% macro is_young_book(first_date_col, as_of_col=none) -%}
    {%- set as_of_col = as_of_col or "(select as_of_date from " ~ ref('int_as_of_date') ~ ")" -%}
    (cast({{ first_date_col }} as date) > cast({{ as_of_col }} as date) - interval '{{ var('young_book_months', 12) }} months')
{%- endmacro %}

{#- Rule 4. Claims money for display: always negative (a cost), whatever sign the source used. -#}
{% macro claims_display_amount(amount_col) -%}
    (-abs({{ amount_col }}))
{%- endmacro %}
