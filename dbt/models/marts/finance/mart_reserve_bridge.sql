-- Reserve bridge (V6 edit 2): why the New Reserves page and the Experience page show different
-- reserve totals, step by step. Grain: tenant x month. All amounts are CLAIMS_RESERVE cost split,
-- inception to date at the latest closed posting period, the same cut-off both pages use.
--   1 gross_new_reserve        reserve booked at sale on contracts ENTERED in the month (New Reserves)
--   2 cancelled_contracts_reserve  less that sale reserve on the contracts since cancelled (effective)
--   3 partial_refund_remainder plus what those cancelled contracts still hold: a cancel refunds only the
--                              unused part, so the rest stays in net written (sale + amendments + cancel
--                              + reinstatement on cancelled contracts)
--   4 other_adjustments        plus amendments, and cancels later reversed by a reinstatement, on the
--                              contracts still on the books
--   = net_written_entered      Experience page, View by Date (entered month)
--   5 timing_in / timing_out   plus net written on contracts EFFECTIVE in the month but entered in another
--                              month, less net written on contracts entered in the month but effective in
--                              another (entered vs effective date)
--   = net_written_effective    computed on its own, straight from the ledger by effective month
--   residual                   net_written_effective - (1 + 2 + 3 + 4 + 5); must be 0.00
--                              (test assert_reserve_bridge_closes, which also ties step 1 to the New
--                              Reserves mart and the entered-month total to the Experience mart)
-- V7 edit 23: a cancel the source later reversed (fct_cancellation.is_reversed / is_reversal_row) is
-- not a cancel: its contract stays on the books, and the cancel + reversal pair nets to zero inside
-- other_adjustments. reversed_cancel_reserve shows that pair's net (should be 0.00) and
-- contracts_cancel_reversed how many contracts it touched, so the step is visible, never hidden.
-- Contract counts walk the same way: contracts_gross - contracts_cancelled = contracts_net.
with as_of as (
    select max(posting_period_key) as as_of_period_key
    from {{ ref('dim_posting_period') }}
    where is_closed
),

cancels as (
    select distinct x.contract_key
    from {{ ref('fct_cancellation') }} as x
    cross join as_of as a
    where x.is_effective_cancel and x.posting_period_key <= a.as_of_period_key
),

reversed as (
    select distinct x.contract_key
    from {{ ref('fct_cancellation') }} as x
    cross join as_of as a
    where (x.is_reversed or x.is_reversal_row) and x.posting_period_key <= a.as_of_period_key
),

per_contract as (
    select
        d.contract_key,
        coalesce(sum(d.amount) filter (
            where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'SALE'), 0) as sale_reserve,
        coalesce(sum(d.amount) filter (where d.disbursement_type = 'CLAIMS_RESERVE'), 0) as net_reserve,
        coalesce(sum(d.amount) filter (
            where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'CANCEL'), 0) as cancel_reserve
    from {{ ref('fct_disbursement') }} as d
    cross join as_of as a
    where d.posting_period_key <= a.as_of_period_key and d.contract_key::varchar <> '-1'
    group by 1
    -- same contract set as the Experience page: the sale has posted by the cut-off
    having bool_or(d.txn_type = 'SALE')
),

contracts as (
    select
        k.tenant_key,
        cast(date_trunc('month', k.entered_date) as date) as entered_month,
        cast(date_trunc('month', k.effective_date) as date) as effective_month,
        c.sale_reserve,
        c.net_reserve,
        x.contract_key is not null as is_cancelled,
        rv.contract_key is not null and x.contract_key is null as is_cancel_reversed,
        c.cancel_reserve
    from per_contract as c
    inner join {{ ref('dim_contract') }} as k using (contract_key)
    left join cancels as x using (contract_key)
    left join reversed as rv using (contract_key)
),

entered as (
    select
        tenant_key,
        entered_month as month_start,
        sum(sale_reserve) as gross_new_reserve,
        coalesce(-sum(sale_reserve) filter (where is_cancelled), 0) as cancelled_contracts_reserve,
        coalesce(sum(net_reserve) filter (where is_cancelled), 0) as partial_refund_remainder,
        coalesce(sum(net_reserve - sale_reserve) filter (where not is_cancelled), 0) as other_adjustments,
        sum(net_reserve) as net_written_entered,
        count(*) as contracts_gross,
        count(*) filter (where is_cancelled) as contracts_cancelled,
        count(*) filter (where is_cancel_reversed) as contracts_cancel_reversed,
        coalesce(sum(cancel_reserve) filter (where is_cancel_reversed), 0) as reversed_cancel_reserve,
        coalesce(sum(net_reserve) filter (where effective_month <> entered_month), 0) as timing_out
    from contracts
    group by 1, 2
),

effective as (
    select
        tenant_key,
        effective_month as month_start,
        sum(net_reserve) as net_written_effective,
        coalesce(sum(net_reserve) filter (where effective_month <> entered_month), 0) as timing_in
    from contracts
    group by 1, 2
),

months as (
    select tenant_key, month_start from entered
    union
    select tenant_key, month_start from effective
),

steps as (
    select
        m.tenant_key,
        m.month_start,
        coalesce(e.gross_new_reserve, 0) as gross_new_reserve,
        coalesce(e.cancelled_contracts_reserve, 0) as cancelled_contracts_reserve,
        coalesce(e.partial_refund_remainder, 0) as partial_refund_remainder,
        coalesce(e.other_adjustments, 0) as other_adjustments,
        coalesce(e.net_written_entered, 0) as net_written_entered,
        coalesce(f.timing_in, 0) as timing_in,
        -coalesce(e.timing_out, 0) as timing_out,
        coalesce(f.net_written_effective, 0) as net_written_effective,
        coalesce(e.contracts_gross, 0) as contracts_gross,
        coalesce(e.contracts_cancelled, 0) as contracts_cancelled,
        coalesce(e.contracts_cancel_reversed, 0) as contracts_cancel_reversed,
        coalesce(e.reversed_cancel_reserve, 0) as reversed_cancel_reserve
    from months as m
    left join entered as e using (tenant_key, month_start)
    left join effective as f using (tenant_key, month_start)
)

select
    a.as_of_period_key,
    s.tenant_key,
    s.month_start,
    s.gross_new_reserve,
    s.cancelled_contracts_reserve,
    s.partial_refund_remainder,
    s.other_adjustments,
    s.net_written_entered,
    s.timing_in,
    s.timing_out,
    s.net_written_effective,
    s.net_written_effective - (s.gross_new_reserve + s.cancelled_contracts_reserve + s.partial_refund_remainder
        + s.other_adjustments + s.timing_in + s.timing_out) as residual,
    s.contracts_gross,
    s.contracts_cancelled,
    s.contracts_gross - s.contracts_cancelled as contracts_net,
    s.contracts_cancel_reversed,
    round(s.reversed_cancel_reserve, 2) as reversed_cancel_reserve,
    {{ audit_columns() }}
from steps as s
cross join as_of as a
