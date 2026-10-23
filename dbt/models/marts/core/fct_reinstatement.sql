-- Grain: one reinstatement request. reversal_amount is the REINSTATE transaction (positive:
-- it re-books what the cancellation reversed); only APPROVED reinstatements carry money.
with reinstate_txn as (
    select contract_key, posted_at, amount, posting_period_key
    from {{ ref('int_transactions_posted') }}
    where txn_type = 'REINSTATE'
)

select
    md5('admin_core|reinstatement|' || cast(r.reinstatement_id as varchar)) as reinstatement_key,
    r.reinstatement_id,
    md5('admin_core|cancel|' || cast(r.cancel_id as varchar)) as cancel_key,
    k.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    r.status,
    r.reinstated_at,
    {{ date_key('r.reinstated_at') }} as reinstated_date_key,
    coalesce(t.posting_period_key, {{ posting_period_key('r.reinstated_at', 'pp') }}) as posting_period_key,
    {{ period_start_date('coalesce(t.posting_period_key, ' ~ posting_period_key('r.reinstated_at', 'pp') ~ ')') }} as posting_period_start_date,
    coalesce(case when r.status = 'APPROVED' then t.amount end, 0) as reversal_amount,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__reinstatement') }} as r
inner join {{ ref('dim_contract') }} as k
    on k.contract_key = {{ contract_key("'admin_core'", 'r.contract_id') }}
left join reinstate_txn as t
    on t.contract_key = k.contract_key and r.status = 'APPROVED'
{{ posting_period_join('r.created_at', 'pp') }}
where not r.is_deleted
qualify row_number() over (partition by r.reinstatement_id order by abs(datediff('day', t.posted_at, r.reinstated_at))) = 1
