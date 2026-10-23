-- Diagnostic: no demo-dealer or test-range row reaches any mart. Exclusion happens once, in
-- int_reportable_scope; this proves every downstream model honoured it. One row per leak.
{% set contract_models = [
    'dim_contract', 'dim_contract_terms', 'dim_contract_finance', 'dim_contract_vehicle',
    'dim_contract_actuarial_class', 'dim_contract_claim_summary', 'dim_claim',
    'fct_transaction', 'fct_disbursement', 'fct_gross_premium', 'fct_cancellation',
    'fct_reinstatement', 'fct_claim_payment_line', 'fct_claim_history', 'fct_cash_transaction',
    'fct_commission', 'fct_contract_monthly', 'fct_contract_sale', 'fct_claim',
    'fct_call_center_contact'] %}

with excluded as (
    select contract_key from {{ ref('int_reportable_scope') }} where not is_reportable
),

excluded_dealers as (
    select dealer_number from {{ ref('int_reportable_dealers') }} where not is_reportable
),

leaks as (
    {% for m in contract_models %}
    select '{{ m }}' as model_name, count(*) as leaked_rows
    from {{ ref(m) }}
    where contract_key in (select contract_key from excluded)
    union all
    {% endfor %}
    select 'dim_dealer', count(*)
    from {{ ref('dim_dealer') }}
    where dealer_number in (select dealer_number from excluded_dealers)
)

select * from leaks where leaked_rows > 0
