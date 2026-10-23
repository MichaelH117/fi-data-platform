-- posting_period_start_date on every financial fact is the first day of its posting_period_key
-- and matches dim_posting_period.period_start (no fact needs the period join for a date).
{% set facts = ['fct_transaction', 'fct_disbursement', 'fct_earned_disbursement', 'fct_gross_premium',
                'fct_cancellation', 'fct_reinstatement', 'fct_claim_payment_line', 'fct_claim_history',
                'fct_cash_transaction', 'fct_commission', 'fct_contract_monthly'] %}
{% for f in facts %}
select '{{ f }}' as fact, f.posting_period_key, f.posting_period_start_date
from {{ ref(f) }} as f
left join {{ ref('dim_posting_period') }} as p using (posting_period_key)
where f.posting_period_start_date is null
   or {{ yyyymm('f.posting_period_start_date') }} <> f.posting_period_key
   or extract(day from f.posting_period_start_date) <> 1
   or (p.posting_period_key is not null and p.period_start <> f.posting_period_start_date)
{% if not loop.last %}union all{% endif %}
{% endfor %}
