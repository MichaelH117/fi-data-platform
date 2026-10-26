-- Experience report with a "View by" switch (V6 edit 1): what was written, what is earned, what was
-- paid. Grain: tenant x entered month x product x dealer x reportable state x make x term x
-- participation entity x effective year; every View by (date, product, dealer, state, plan, make,
-- effective year, term) is a group-by over this one table, so all views add up to the same total.
-- Reserve basis, like the cession page: net_written = CLAIMS_RESERVE cost split (sale, amendment,
-- cancel, reinstatement), earned = earned claims reserve, unearned = net_written - earned,
-- claims_paid = claim payments. Everything is inception-to-date at the latest CLOSED posting period
-- (as_of_period_key), so a published figure never moves. Contracts count when their sale has posted
-- by then; contracts_net = written - effectively cancelled (the "net contract count").
-- participation_entity_key = '-1' when the dealer has no entity or the contract was entered after the
-- entity's run-off date (mart_participation_contract).
with as_of as (
    select max(posting_period_key) as as_of_period_key
    from {{ ref('dim_posting_period') }}
    where is_closed
),

ledger as (
    select
        d.contract_key,
        sum(d.amount) filter (where d.disbursement_type = 'CLAIMS_RESERVE') as net_written,
        coalesce(sum(d.amount) filter (
            where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'SALE'), 0) as gross_written,
        bool_or(d.txn_type = 'SALE') as has_sale
    from {{ ref('fct_disbursement') }} as d
    cross join as_of as a
    where d.posting_period_key <= a.as_of_period_key
    group by 1
),

earned as (
    select e.contract_key, sum(e.earned_reserve) as earned
    from {{ ref('fct_earned_disbursement') }} as e
    cross join as_of as a
    where e.disbursement_type = 'CLAIMS_RESERVE' and e.posting_period_key <= a.as_of_period_key
    group by 1
),

claims as (
    select
        l.contract_key,
        sum(l.paid_amount) as claims_paid,
        count(distinct l.claim_key) filter (where l.paid_amount > 0) as claims_count
    from {{ ref('fct_claim_payment_line') }} as l
    cross join as_of as a
    where l.posting_period_key <= a.as_of_period_key
    group by 1
),

cancels as (
    select distinct x.contract_key
    from {{ ref('fct_cancellation') }} as x
    cross join as_of as a
    where x.is_effective_cancel and x.posting_period_key <= a.as_of_period_key
),

contracts as (
    select
        k.contract_key,
        k.tenant_key,
        cast(date_trunc('month', k.entered_date) as date) as entered_month,
        year(k.effective_date) as effective_year,
        k.product_key,
        k.dealer_key,
        coalesce(k.reportable_state, 'UNKNOWN') as reportable_state,
        coalesce(v.vehicle_make, 'UNKNOWN') as vehicle_make,
        t.term_months,
        coalesce(m.participation_entity_key, '-1') as participation_entity_key,
        l.net_written,
        l.gross_written,
        coalesce(e.earned, 0) as earned,
        coalesce(c.claims_paid, 0) as claims_paid,
        coalesce(c.claims_count, 0) as claims_count,
        x.contract_key is not null as is_cancelled
    from {{ ref('dim_contract') }} as k
    inner join ledger as l using (contract_key)
    left join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    left join {{ ref('dim_contract_terms') }} as t using (contract_key)
    left join {{ ref('mart_participation_contract') }} as m on m.contract_key = k.contract_key and m.is_ceded
    left join earned as e using (contract_key)
    left join claims as c using (contract_key)
    left join cancels as x using (contract_key)
    where l.has_sale and k.contract_key::varchar <> '-1'
)

select
    a.as_of_period_key,
    c.tenant_key,
    c.entered_month,
    year(c.entered_month) as entered_year,
    c.effective_year,
    c.product_key,
    p.product_code,
    p.product as product_name,
    p.category_code,
    p.category,
    p.plan,
    c.dealer_key,
    d.dealer_number,
    d.dealer_name,
    c.reportable_state,
    c.vehicle_make,
    c.term_months,
    c.participation_entity_key,
    count(*) as contracts_written,
    count(*) filter (where c.is_cancelled) as contracts_cancelled,
    count(*) - count(*) filter (where c.is_cancelled) as contracts_net,
    sum(c.gross_written) as gross_written,
    sum(c.net_written) as net_written,
    sum(c.earned) as earned,
    sum(c.net_written) - sum(c.earned) as unearned,
    sum(c.claims_paid) as claims_paid,
    sum(c.claims_count) as claims_count,
    {{ safe_divide('sum(c.earned)', 'sum(c.net_written)') }} as earned_pct,
    {{ safe_divide('count(*) filter (where c.is_cancelled)', 'count(*)') }} as cancel_pct,
    {{ safe_divide('sum(c.claims_paid)', 'sum(c.earned)') }} as loss_ratio,
    {{ audit_columns() }}
from contracts as c
cross join as_of as a
left join {{ ref('dim_product') }} as p using (product_key)
left join {{ ref('dim_dealer') }} as d using (dealer_key)
group by all
