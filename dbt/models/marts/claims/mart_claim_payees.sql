-- Claim payees (V6 edit 18: the "claim payees" export). Grain: claim x payee account holder.
-- Derived from core.fct_claim_payment_line; the payee name and type come from dim_account_holder.
-- Legacy lines have no payee split and land on the unknown member ('-1'), labelled as such.
select
    l.claim_key,
    s.claim_number,
    s.contract_number,
    l.payee_account_holder_key,
    coalesce(h.holder_type, 'UNKNOWN') as payee_type,
    coalesce(h.holder_name, 'Unknown / not split') as payee_name,
    h.state as payee_state,
    count(*) as payment_lines,
    round(sum(l.paid_amount), 2) as paid_amount,
    round(sum(l.pending_amount), 2) as pending_amount,
    round(sum(l.paid_amount + l.pending_amount), 2) as incurred_amount,
    min(l.posted_at) filter (where l.paid_amount > 0) as first_paid_at,
    max(l.posted_at) filter (where l.paid_amount > 0) as last_paid_at,
    {{ audit_columns() }}
from {{ ref('fct_claim_payment_line') }} as l
inner join {{ ref('mart_claim_summary') }} as s on s.claim_key = l.claim_key
left join {{ ref('dim_account_holder') }} as h on h.account_holder_key = l.payee_account_holder_key
group by all
