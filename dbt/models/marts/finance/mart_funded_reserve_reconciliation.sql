-- Funded claims-reserve reconciliation. Grain: tenant x posting period, from the first activity to
-- the as-of period. Nothing here is a plug: each line is computed on its own and the differences
-- must be zero (test fin_reserve_rollforward_ties_to_cession).
--   participation_reserve_balance = sum of mart_cession_experience.reserve_balance (entities)
--   rest_of_book_reserve_balance  = ITD CLAIMS_RESERVE cost split (fct_disbursement) less ITD claim
--                                   payments on contracts with no participation entity, recomputed
--                                   from the core facts (no surplus is released on these contracts)
--   unmatched_claims_paid         = ITD claim payments with no contract key (shown as "of which"; already
--                                   inside the rest of book)
--   book_closing_funded_reserve   = sum of mart_reserve_rollforward.closing_funded_reserve
--   difference                    = book - (participation + rest of book)
--   earned_surplus                = ITD earned claims reserve - ITD claims paid - ITD surplus
--                                   released by distributions, from the facts and the cession mart
--   earned_surplus_difference     = (book closing funded - book closing unearned) - earned_surplus
with entity_contract as (
    -- ceded contracts only: a contract entered after its entity's run-off date is rest of book
    -- (mart_participation_contract, V6 edit 13)
    select contract_key
    from {{ ref('mart_participation_contract') }}
    where is_ceded
),

rest_written as (
    select d.tenant_key, d.posting_period_key, sum(d.amount) as amount
    from {{ ref('fct_disbursement') }} as d
    where d.disbursement_type = 'CLAIMS_RESERVE'
        and d.contract_key not in (select contract_key from entity_contract)
    group by 1, 2
),

claims as (
    select
        l.tenant_key,
        l.posting_period_key,
        sum(l.paid_amount) as paid_all,
        coalesce(sum(l.paid_amount) filter (
            where l.contract_key is null or l.contract_key not in (select contract_key from entity_contract)
        ), 0) as paid_rest,
        coalesce(sum(l.paid_amount) filter (where l.contract_key is null or l.contract_key::varchar = '-1'), 0)
            as paid_unmatched
    from {{ ref('fct_claim_payment_line') }} as l
    group by 1, 2
),

earned as (
    select tenant_key, posting_period_key, sum(earned_reserve) as earned_reserve
    from {{ ref('fct_earned_disbursement') }}
    where disbursement_type = 'CLAIMS_RESERVE'
    group by 1, 2
),

cession as (
    select
        tenant_key,
        posting_period_key,
        sum(reserve_balance) as participation_reserve_balance,
        sum(claims_reserve_released) as surplus_released,
        count(*) as entities
    from {{ ref('mart_cession_experience') }}
    group by 1, 2
),

book as (
    select
        tenant_key,
        posting_period_key,
        sum(closing_funded_reserve) as book_closing_funded_reserve,
        sum(closing_unearned_reserve) as book_closing_unearned_reserve
    from {{ ref('mart_reserve_rollforward') }}
    group by 1, 2
),

keys as (
    select tenant_key, posting_period_key from rest_written
    union select tenant_key, posting_period_key from claims
    union select tenant_key, posting_period_key from earned
    union select tenant_key, posting_period_key from cession
    union select tenant_key, posting_period_key from book
),

as_of as (
    select max(posting_period_key) as as_of_period from {{ ref('fct_transaction') }}
),

spine as (
    select k.tenant_key, p.posting_period_key, p.period_end, p.is_closed
    from (select tenant_key, min(posting_period_key) as first_period_key from keys group by 1) as k
    cross join as_of as a
    inner join {{ ref('dim_posting_period') }} as p
        on p.posting_period_key between k.first_period_key and a.as_of_period
),

rolled as (
    select
        s.tenant_key,
        s.posting_period_key,
        s.period_end,
        s.is_closed,
        coalesce(c.participation_reserve_balance, 0) as participation_reserve_balance,
        coalesce(c.entities, 0) as participation_entities,
        sum(coalesce(rw.amount, 0) - coalesce(cl.paid_rest, 0)) over w as rest_of_book_reserve_balance,
        sum(coalesce(cl.paid_unmatched, 0)) over w as unmatched_claims_paid,
        coalesce(b.book_closing_funded_reserve, 0) as book_closing_funded_reserve,
        coalesce(b.book_closing_unearned_reserve, 0) as book_closing_unearned_reserve,
        sum(coalesce(e.earned_reserve, 0) - coalesce(cl.paid_all, 0) - coalesce(c.surplus_released, 0)) over w
            as earned_surplus
    from spine as s
    left join rest_written as rw using (tenant_key, posting_period_key)
    left join claims as cl using (tenant_key, posting_period_key)
    left join earned as e using (tenant_key, posting_period_key)
    left join cession as c using (tenant_key, posting_period_key)
    left join book as b using (tenant_key, posting_period_key)
    window w as (partition by s.tenant_key order by s.posting_period_key rows unbounded preceding)
)

select
    *,
    participation_reserve_balance + rest_of_book_reserve_balance as reconciled_funded_reserve,
    book_closing_funded_reserve - (participation_reserve_balance + rest_of_book_reserve_balance) as difference,
    book_closing_funded_reserve - book_closing_unearned_reserve - earned_surplus as earned_surplus_difference,
    {{ audit_columns() }}
from rolled
