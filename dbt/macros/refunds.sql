{#- Unexpired share a cancellation refunds, by refund method (the Admin Core rule, applied to both
    systems): FULL_REFUND = 1; R78 = rule of 78 on whole months used; anything else (PRORATED) =
    unexpired days / term days, with a month of 30.4375 days. Rounded to 4 places like the source. -#}
{% macro refund_share(method, age_days, term_months) -%}
    round(case
        when {{ method }} = 'FULL_REFUND' then 1.0
        when {{ method }} = 'R78' then
            ({{ term_months }} - least(ceil({{ age_days }} / 30.4375), {{ term_months }}))
            * ({{ term_months }} - least(ceil({{ age_days }} / 30.4375), {{ term_months }}) + 1)
            / nullif({{ term_months }} * ({{ term_months }} + 1), 0)
        else greatest(0, least(1, 1 - {{ age_days }} / nullif(round({{ term_months }} * 30.4375), 0)))
    end, 4)
{%- endmacro %}
