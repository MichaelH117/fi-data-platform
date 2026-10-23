-- Grain: one cancellation (both systems; reinstated ones included and flagged).
-- Posts in the period of its CANCEL transaction (period lock applies); cancel_date stays the true date.
with cancel_txn as (
    select contract_key, posting_period_key, posted_at, source_txn_id, source_system
    from {{ ref('int_transactions_posted') }}
    where txn_type = 'CANCEL'
)

select
    x.cancel_key,
    x.source_system,
    x.source_cancel_id,
    x.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    x.cancel_date,
    {{ date_key('x.cancel_date') }} as cancel_date_key,
    coalesce(t.posting_period_key, {{ posting_period_key('x.cancel_date', 'pp') }}) as posting_period_key,
    {{ period_start_date('coalesce(t.posting_period_key, ' ~ posting_period_key('x.cancel_date', 'pp') ~ ')') }} as posting_period_start_date,
    x.initiated_by,
    x.reason,
    x.method,
    x.odometer,
    x.contract_age_days,
    x.refund_pct,
    x.cancel_fee,
    x.customer_refund,
    x.admin_portion,
    x.dealer_portion,
    x.coverage_cost_refund,
    x.dealer_profit_refund,
    x.admin_withheld_claims,
    x.is_reinstated,
    x.is_effective_cancel,
    x.is_amount_sign_fixed,
    {{ audit_columns() }}
from {{ ref('int_cancellations_conformed') }} as x
inner join {{ ref('dim_contract') }} as k using (contract_key)
left join cancel_txn as t
    on t.contract_key = x.contract_key
    and (
        (x.source_system = 'legacy_admin' and t.source_txn_id = 'CANCEL-' || x.source_cancel_id)
        or (x.source_system = 'admin_core' and t.source_system = 'admin_core')
    )
{{ posting_period_join('x.recorded_at', 'pp') }}
qualify row_number() over (partition by x.cancel_key order by t.posted_at) = 1
