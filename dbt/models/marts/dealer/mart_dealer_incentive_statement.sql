-- V7 edit 46: dealer incentive statement per remittance. "What we owe your store for this payment,
-- line by line." Grain: tenant x dealer x remittance x product category.
-- The incentive is the PRODUCER_INCENTIVE cost-split bucket (fct_disbursement.incentive_group).
-- Which remittance a line lands on:
--   SALE lines          the remittance that paid for the contract (fct_cash_transaction.remit_batch_id);
--                       a contract not remitted yet waits on NOT_YET_REMITTED; a converted legacy
--                       contract has no remittance record (paid before the platform): LEGACY
--   every other line    (cancel, reinstatement, amendment, anything else) the dealer's first remittance
--                       received on or after the line posted; none yet = NEXT_REMITTANCE
-- Sections: commissions (SALE), cancels (CANCEL), reinstatements (REINSTATE), amendments (AMEND),
-- other_adjustments (any other transaction type). total_due = the sum of the five sections
-- (tested within 5 x half a cent of rounding), and the statement as a whole ties to the ledger (assert_incentive_statement_ties_to_ledger).
with lines as (
    select
        d.disbursement_key,
        d.tenant_key,
        d.dealer_key,
        d.contract_key,
        d.source_system,
        d.txn_type,
        d.posted_at,
        d.amount,
        coalesce(p.category_code, 'UNKNOWN') as category_code
    from {{ ref('fct_disbursement') }} as d
    left join {{ ref('dim_product') }} as p on p.product_key = d.product_key
    where d.incentive_group = 'PRODUCER_INCENTIVE'
),

cash as (
    select contract_key, min(remit_batch_id) as remit_batch_id
    from {{ ref('fct_cash_transaction') }}
    group by 1
),

batches as (
    select dealer_key, remit_batch_id, min(received_at) as received_at
    from {{ ref('fct_cash_transaction') }}
    group by 1, 2
),

batch_dates as (
    select remit_batch_id, min(received_at) as received_at from batches group by 1
),

sale_lines as (
    select
        l.*,
        coalesce(c.remit_batch_id,
                 case when l.source_system = 'legacy_admin' then 'LEGACY_NO_REMIT_RECORD' else 'NOT_YET_REMITTED' end)
            as remit_batch_id
    from lines as l
    left join cash as c using (contract_key)
    where l.txn_type = 'SALE'
),

adjustment_lines as (
    select l.*, coalesce(b.remit_batch_id, 'NEXT_REMITTANCE') as remit_batch_id
    from (select * from lines where txn_type <> 'SALE') as l
    asof left join batches as b
        on b.dealer_key = l.dealer_key and l.posted_at <= b.received_at
),

assigned as (
    select * from sale_lines
    union all by name
    select * from adjustment_lines
)

select
    a.tenant_key,
    a.dealer_key,
    d.dealer_number,
    d.dealer_name,
    a.remit_batch_id,
    case a.remit_batch_id when 'NOT_YET_REMITTED' then 'NOT_YET_REMITTED'
                          when 'NEXT_REMITTANCE' then 'NEXT_REMITTANCE'
                          when 'LEGACY_NO_REMIT_RECORD' then 'LEGACY' else 'REMITTED' end as statement_status,
    cast(bd.received_at as date) as remit_received_date,
    a.category_code as product_type,
    round(coalesce(sum(a.amount) filter (where a.txn_type = 'SALE'), 0), 2) as commissions,
    count(distinct a.contract_key) filter (where a.txn_type = 'SALE') as commission_contracts,
    round(coalesce(sum(a.amount) filter (where a.txn_type = 'CANCEL'), 0), 2) as cancels,
    count(distinct a.contract_key) filter (where a.txn_type = 'CANCEL') as cancel_contracts,
    round(coalesce(sum(a.amount) filter (where a.txn_type = 'REINSTATE'), 0), 2) as reinstatements,
    count(distinct a.contract_key) filter (where a.txn_type = 'REINSTATE') as reinstatement_contracts,
    round(coalesce(sum(a.amount) filter (where a.txn_type = 'AMEND'), 0), 2) as amendments,
    count(distinct a.contract_key) filter (where a.txn_type = 'AMEND') as amendment_contracts,
    round(coalesce(sum(a.amount) filter (
        where a.txn_type not in ('SALE', 'CANCEL', 'REINSTATE', 'AMEND')), 0), 2) as other_adjustments,
    count(distinct a.contract_key) filter (
        where a.txn_type not in ('SALE', 'CANCEL', 'REINSTATE', 'AMEND')) as other_contracts,
    round(sum(a.amount), 2) as total_due,  -- summed straight from the lines, so the section test means something
    {{ audit_columns() }}
from assigned as a
left join {{ ref('dim_dealer') }} as d on d.dealer_key = a.dealer_key
left join batch_dates as bd on bd.remit_batch_id = a.remit_batch_id
group by all
