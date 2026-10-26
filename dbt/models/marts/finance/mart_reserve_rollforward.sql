-- Claims-reserve roll-forward. Grain: tenant x product category x posting period, every month from
-- the first activity to the as-of period. Two balances roll:
--   funded reserve: opening + written + amended - cancelled + reinstated - claims paid
--                   - surplus released (profit-share distributions) = closing
--   unearned reserve: opening + net written - earned = closing
-- Every flow is the CLAIMS_RESERVE bucket of the cost split (fct_disbursement / fct_earned_disbursement)
-- or a claim payment line, posted by posting_period_key. Surplus released is the part of each
-- participation entity's distribution not covered by its profit-share reserve
-- (mart_cession_experience.claims_reserve_released), spread over tenant x category by the entity's
-- ITD earned claims reserve in each tenant x category up to the release period.
-- So closing funded - closing unearned = ITD earned - claims paid - surplus released.
with prod as (
    select product_key, category_code, category from {{ ref('dim_product') }}
),

ledger as (
    select
        d.tenant_key,
        p.category_code,
        d.posting_period_key,
        sum(d.amount) filter (where d.txn_type = 'SALE') as reserve_written,
        sum(d.amount) filter (where d.txn_type = 'AMEND') as reserve_amended,
        sum(d.amount) filter (where d.txn_type = 'CANCEL') as reserve_cancelled,
        sum(d.amount) filter (where d.txn_type = 'REINSTATE') as reserve_reinstated
    from {{ ref('fct_disbursement') }} as d
    inner join prod as p using (product_key)
    where d.disbursement_type = 'CLAIMS_RESERVE'
    group by 1, 2, 3
),

earned as (
    select e.tenant_key, p.category_code, e.posting_period_key, sum(e.earned_reserve) as reserve_earned
    from {{ ref('fct_earned_disbursement') }} as e
    inner join prod as p using (product_key)
    where e.disbursement_type = 'CLAIMS_RESERVE'
    group by 1, 2, 3
),

claims as (
    select l.tenant_key, p.category_code, l.posting_period_key, sum(l.paid_amount) as claims_paid
    from {{ ref('fct_claim_payment_line') }} as l
    inner join prod as p using (product_key)
    group by 1, 2, 3
),

entity_contract as (
    -- ceded contracts only (run-off rule, mart_participation_contract)
    select contract_key, participation_entity_key
    from {{ ref('mart_participation_contract') }}
    where is_ceded
),

release as (
    select participation_entity_key, posting_period_key, claims_reserve_released
    from {{ ref('mart_cession_experience') }}
    where claims_reserve_released <> 0
),

release_weight as (
    select
        r.participation_entity_key,
        r.posting_period_key,
        e.tenant_key,
        p.category_code,
        any_value(r.claims_reserve_released) as claims_reserve_released,
        sum(e.earned_reserve) as earned_to_date
    from release as r
    inner join entity_contract as c using (participation_entity_key)
    inner join {{ ref('fct_earned_disbursement') }} as e
        on e.contract_key = c.contract_key
        and e.disbursement_type = 'CLAIMS_RESERVE'
        and e.posting_period_key <= r.posting_period_key
    inner join prod as p on p.product_key = e.product_key
    group by 1, 2, 3, 4
),

release_share as (
    select
        *,
        claims_reserve_released * earned_to_date
            / sum(earned_to_date) over (partition by participation_entity_key, posting_period_key)
            as surplus_released
    from release_weight
),

released as (
    select tenant_key, category_code, posting_period_key, sum(surplus_released) as surplus_released
    from release_share
    group by 1, 2, 3
),

keys as (
    select tenant_key, category_code, posting_period_key from ledger
    union select tenant_key, category_code, posting_period_key from earned
    union select tenant_key, category_code, posting_period_key from claims
    union select tenant_key, category_code, posting_period_key from released
),

as_of as (
    select max(posting_period_key) as as_of_period from {{ ref('fct_transaction') }}
),

spine as (
    select k.tenant_key, k.category_code, p.posting_period_key, p.period_end, p.is_closed
    from (
        select tenant_key, category_code, min(posting_period_key) as first_period_key
        from keys group by 1, 2
    ) as k
    cross join as_of as a
    inner join {{ ref('dim_posting_period') }} as p
        on p.posting_period_key between k.first_period_key and a.as_of_period
),

flows as (
    select
        s.*,
        coalesce(l.reserve_written, 0) as reserve_written,
        coalesce(l.reserve_amended, 0) as reserve_amended,
        coalesce(l.reserve_cancelled, 0) as reserve_cancelled,
        coalesce(l.reserve_reinstated, 0) as reserve_reinstated,
        coalesce(c.claims_paid, 0) as claims_paid,
        coalesce(sr.surplus_released, 0) as surplus_released,
        coalesce(e.reserve_earned, 0) as reserve_earned
    from spine as s
    left join ledger as l using (tenant_key, category_code, posting_period_key)
    left join earned as e using (tenant_key, category_code, posting_period_key)
    left join claims as c using (tenant_key, category_code, posting_period_key)
    left join released as sr using (tenant_key, category_code, posting_period_key)
),

net as (
    select
        *,
        reserve_written + reserve_amended + reserve_cancelled + reserve_reinstated as net_reserve_written
    from flows
),

rolled as (
    select
        *,
        sum(net_reserve_written - claims_paid - surplus_released) over w as closing_funded_reserve,
        sum(net_reserve_written - reserve_earned) over w as closing_unearned_reserve
    from net
    window w as (partition by tenant_key, category_code order by posting_period_key rows unbounded preceding)
)

select
    r.tenant_key,
    r.category_code,
    p.category,
    r.posting_period_key,
    r.period_end,
    r.is_closed,
    r.closing_funded_reserve - (r.net_reserve_written - r.claims_paid - r.surplus_released)
        as opening_funded_reserve,
    r.reserve_written,
    r.reserve_amended,
    r.reserve_cancelled,
    r.reserve_reinstated,
    r.net_reserve_written,
    r.claims_paid,
    r.surplus_released,
    r.closing_funded_reserve,
    r.closing_unearned_reserve - (r.net_reserve_written - r.reserve_earned) as opening_unearned_reserve,
    r.reserve_earned,
    r.closing_unearned_reserve,
    {{ audit_columns() }}
from rolled as r
left join (select category_code, min(category) as category from prod group by 1) as p using (category_code)
