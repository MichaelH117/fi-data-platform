-- Both systems refund on one basis: refund_pct is the refund method's unexpired share (pro rata,
-- rule of 78, or full) within 0.002 (about 4 days on a 5-year term:
-- the source counts contract age from its own clock), and the customer refund is the coverage-cost plus dealer
-- portions less the cancel fee and withheld claims (never below zero), within a cent per side.
select
    x.cancel_key,
    x.source_system,
    x.method,
    x.refund_pct,
    {{ refund_share('x.method', 'x.contract_age_days', 'c.term_months') }} as method_share,
    x.customer_refund,
    x.admin_portion + x.dealer_portion - x.cancel_fee - x.admin_withheld_claims as expected_refund
from {{ ref('int_cancellations_conformed') }} as x
inner join {{ ref('int_contracts_conformed') }} as c using (contract_key)
where abs(x.refund_pct - {{ refund_share('x.method', 'x.contract_age_days', 'c.term_months') }}) > 0.002
   or abs(x.customer_refund
          - greatest(0, x.admin_portion + x.dealer_portion - x.cancel_fee - x.admin_withheld_claims)) > 0.02
