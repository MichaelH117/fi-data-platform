-- Diagnostic: posting-period integrity. Every financial fact row carries a posting_period_key that
-- exists in dim_posting_period, so every fact reconciles to the ledger. One row per failing fact.
{% set facts = [
    'fct_transaction', 'fct_disbursement', 'fct_earned_disbursement', 'fct_gross_premium',
    'fct_cancellation', 'fct_reinstatement', 'fct_claim_payment_line', 'fct_claim_history',
    'fct_cash_transaction', 'fct_commission', 'fct_contract_monthly'] %}

with checks as (
    {% for f in facts %}
    select '{{ f }}' as fact_name, count(*) as bad_rows
    from {{ ref(f) }} as x
    where x.posting_period_key is null
       or x.posting_period_key not in (select posting_period_key from {{ ref('dim_posting_period') }})
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select * from checks where bad_rows > 0
