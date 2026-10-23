-- Cession / experience report. Grain: participation entity x posting period, from the entity's first
-- activity to the as-of period (months without activity are kept, so ITD columns never skip).
-- A contract belongs to the participation entity of its dealer (one entity per dealer). Reserve
-- dollars come from the cost-split ledger (fct_disbursement), earning from fct_earned_disbursement
-- (CLAIMS_RESERVE bucket), claims from fct_claim_payment_line. Statement terms come from the seed
-- participation_structure_terms. loss_ratio = incurred / earned reserve (the metrics.yml posted_loss_ratio
-- definition; metrics.yml loss_ratio uses earned premium).
-- Distributions (seed profit_share_payout_schedule) are computed here, not in the statement, because
-- they leave the reserve: once a year, in payout_month, the share earned up to the prior December is
-- paid (a year-end figure is never paid twice). Each payout is funded first from the profit-share
-- reserve funded ITD; the rest is surplus released from the claims reserve, so
-- reserve_balance = written - claims paid - claims-reserve surplus released.
with terms as (
    select * from {{ ref('participation_structure_terms') }}
),

schedule as (
    select structure_type as entity_type, payout_month from {{ ref('profit_share_payout_schedule') }}
),

entity as (
    select
        e.participation_entity_key,
        e.entity_id,
        e.entity_name,
        e.entity_type,
        e.dealer_key,
        e.tenant_key,
        e.reinsurer_name,
        e.effective_date,
        coalesce(e.profit_share_pct, t.default_dealer_share_pct) as dealer_share_pct,
        t.owns_claims_reserve,
        t.ceding_fee_pct,
        t.min_seasoning_months
    from {{ ref('dim_participation_entity') }} as e
    inner join terms as t on t.structure_type = e.entity_type
),

entity_contract as (
    select k.contract_key, en.participation_entity_key
    from {{ ref('dim_contract') }} as k
    inner join entity as en using (dealer_key)
),

ledger as (
    select
        c.participation_entity_key,
        d.posting_period_key,
        sum(d.amount) filter (where d.disbursement_type = 'CLAIMS_RESERVE') as written_reserve,
        sum(d.amount) filter (
            where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'CANCEL'
        ) as cancelled_reserve,
        sum(d.amount) filter (where d.disbursement_type = 'PROFIT_SHARE_RESERVE') as written_profit_share_reserve
    from {{ ref('fct_disbursement') }} as d
    inner join entity_contract as c using (contract_key)
    group by 1, 2
),

earned as (
    select c.participation_entity_key, e.posting_period_key, sum(e.earned_reserve) as earned_reserve
    from {{ ref('fct_earned_disbursement') }} as e
    inner join entity_contract as c using (contract_key)
    where e.disbursement_type = 'CLAIMS_RESERVE'
    group by 1, 2
),

claims as (
    select
        c.participation_entity_key,
        l.posting_period_key,
        sum(l.paid_amount) as paid_claims,
        sum(l.pending_amount) as case_reserve_change,
        sum(l.incurred_change) as incurred_claims,
        count(distinct l.claim_key) filter (where l.paid_amount > 0) as claims_paid_count
    from {{ ref('fct_claim_payment_line') }} as l
    inner join entity_contract as c using (contract_key)
    group by 1, 2
),

sales as (
    select c.participation_entity_key, g.posting_period_key, sum(g.contracts_written) as contracts_written
    from {{ ref('fct_gross_premium') }} as g
    inner join entity_contract as c using (contract_key)
    group by 1, 2
),

activity as (
    select participation_entity_key, posting_period_key from ledger
    union all select participation_entity_key, posting_period_key from earned
    union all select participation_entity_key, posting_period_key from claims
),

as_of as (
    select max(posting_period_key) as as_of_period from {{ ref('fct_transaction') }}
),

first_period as (
    select participation_entity_key, min(posting_period_key) as first_period_key
    from activity
    group by 1
),

spine as (
    select en.*, p.posting_period_key, p.period_start, p.period_end, p.is_closed, f.first_period_key
    from entity as en
    inner join first_period as f using (participation_entity_key)
    cross join as_of as a
    inner join {{ ref('dim_posting_period') }} as p
        on p.posting_period_key between f.first_period_key and a.as_of_period
),

monthly as (
    select
        s.*,
        coalesce(sa.contracts_written, 0) as contracts_written,
        coalesce(l.written_reserve, 0) as written_reserve,
        coalesce(l.cancelled_reserve, 0) as cancelled_reserve,
        coalesce(l.written_profit_share_reserve, 0) as written_profit_share_reserve,
        coalesce(e.earned_reserve, 0) as earned_reserve,
        coalesce(cl.paid_claims, 0) as paid_claims,
        coalesce(cl.case_reserve_change, 0) as case_reserve_change,
        coalesce(cl.incurred_claims, 0) as incurred_claims,
        coalesce(cl.claims_paid_count, 0) as claims_paid_count
    from spine as s
    left join ledger as l using (participation_entity_key, posting_period_key)
    left join earned as e using (participation_entity_key, posting_period_key)
    left join claims as cl using (participation_entity_key, posting_period_key)
    left join sales as sa using (participation_entity_key, posting_period_key)
),

itd as (
    select
        *,
        sum(written_reserve) over w as itd_written_reserve,
        sum(earned_reserve) over w as itd_earned_reserve,
        sum(paid_claims) over w as itd_paid_claims,
        sum(case_reserve_change) over w as itd_case_reserve,
        sum(incurred_claims) over w as itd_incurred_claims,
        sum(written_profit_share_reserve) over w as itd_profit_share_reserve_funded,
        sum(contracts_written) over w as itd_contracts_written
    from monthly
    window w as (partition by participation_entity_key order by posting_period_key rows unbounded preceding)
),

result as (
    select
        *,
        ceding_fee_pct * itd_earned_reserve as itd_ceding_fee,
        itd_earned_reserve - itd_incurred_claims - ceding_fee_pct * itd_earned_reserve as itd_underwriting_result,
        datediff('month', greatest(effective_date, first_period_start), period_end) as months_seasoned
    from (
        select *, min(period_start) over (partition by participation_entity_key) as first_period_start
        from itd
    )
),

-- Paid-through amount at each December: the highest year-end share earned so far (so a year-end
-- amount is paid once, and a later drop is not paid back).
share as (
    select
        *,
        case
            when months_seasoned >= min_seasoning_months
                then greatest(itd_underwriting_result, 0) * dealer_share_pct
            else 0
        end as itd_profit_share_due
    from result
),

year_end as (
    select
        participation_entity_key,
        posting_period_key as year_end_period_key,
        max(itd_profit_share_due) over (
            partition by participation_entity_key order by posting_period_key rows unbounded preceding
        ) as paid_through
    from share
    where posting_period_key % 100 = 12
),

paid as (
    select
        sh.*,
        coalesce(y.paid_through, 0) as itd_distributions_paid,
        sc.payout_month
    from share as sh
    left join schedule as sc using (entity_type)
    left join year_end as y
        on y.participation_entity_key = sh.participation_entity_key
        and y.year_end_period_key = case
            when sh.posting_period_key % 100 >= coalesce(sc.payout_month, 13)
                then (sh.posting_period_key // 100 - 1) * 100 + 12
            else (sh.posting_period_key // 100 - 2) * 100 + 12
        end
),

-- Funding waterfall, evaluated at each payout: profit-share reserve funded ITD first, then the claims
-- reserve. The released amount is carried (and never goes down) between payouts.
payout as (
    select
        *,
        itd_distributions_paid > coalesce(lag(itd_distributions_paid) over w, 0) as is_payout
    from paid
    window w as (partition by participation_entity_key order by posting_period_key)
),

released as (
    select
        *,
        max(
            case
                when is_payout then greatest(itd_distributions_paid - itd_profit_share_reserve_funded, 0)
                else 0
            end
        ) over (
            partition by participation_entity_key order by posting_period_key rows unbounded preceding
        ) as itd_claims_reserve_released
    from payout
),

flows as (
    select
        *,
        itd_distributions_paid
            - coalesce(lag(itd_distributions_paid) over w, 0) as distributions_paid,
        itd_claims_reserve_released
            - coalesce(lag(itd_claims_reserve_released) over w, 0) as claims_reserve_released
    from released
    window w as (partition by participation_entity_key order by posting_period_key)
)

select
    participation_entity_key,
    entity_id,
    entity_name,
    entity_type,
    dealer_key,
    tenant_key,
    reinsurer_name,
    owns_claims_reserve,
    dealer_share_pct,
    ceding_fee_pct,
    min_seasoning_months,
    posting_period_key,
    period_start,
    period_end,
    is_closed,
    contracts_written,
    written_reserve,
    cancelled_reserve,
    earned_reserve,
    paid_claims,
    case_reserve_change,
    incurred_claims,
    claims_paid_count,
    {{ safe_divide('incurred_claims', 'earned_reserve') }} as loss_ratio,
    itd_contracts_written,
    itd_written_reserve,
    itd_earned_reserve,
    itd_paid_claims,
    itd_case_reserve,
    itd_incurred_claims,
    {{ safe_divide('itd_incurred_claims', 'itd_earned_reserve') }} as itd_loss_ratio,
    -- Funded claims reserve still held for the entity's contracts: everything written (net of cancels)
    -- less claims paid less the surplus released to the dealer as distributions.
    itd_written_reserve - itd_paid_claims - itd_claims_reserve_released as reserve_balance,
    itd_written_reserve - itd_earned_reserve as unearned_reserve,
    itd_ceding_fee,
    itd_underwriting_result,
    months_seasoned,
    months_seasoned >= min_seasoning_months as is_seasoned,
    -- Gross share earned since inception (before distributions); the statement's opening/closing due
    -- nets the distributions out.
    itd_profit_share_due,
    distributions_paid,
    itd_distributions_paid,
    itd_profit_share_due - itd_distributions_paid as closing_profit_share_due,
    claims_reserve_released,
    itd_claims_reserve_released,
    written_profit_share_reserve,
    itd_profit_share_reserve_funded,
    {{ audit_columns() }}
from flows
