-- Profit-share statement. Grain: participation entity x posting period (same rows as
-- mart_cession_experience). The ITD profit share earned is re-computed every period from the ITD
-- underwriting result (itd_profit_share_earned). Distributions come from mart_cession_experience (seed
-- profit_share_payout_schedule: once a year, in payout_month, the share earned up to the prior
-- December is paid; a year-end figure is never paid twice). The statement shows opening due, this
-- period's movement, distributions paid and closing due (= earned ITD - distributed ITD).
-- Closed periods are FINAL and never restated; the open period is PRELIMINARY.
-- due_category splits what the administrator owes from what it does not:
--   PAYABLE: PROFIT_SHARE structures; the obligor holds the reserve and owes the dealer its share.
--   DEALER_ENTITY_SURPLUS: reserve-owning structures (CFC, NCFC, PORC, retro, dealer-owned obligor);
--   the surplus already sits in the dealer's own reinsurer or obligor, so it is not an administrator
--   liability.
with ce as (
    select * from {{ ref('mart_cession_experience') }}
),

-- Distributions (once a year, seed profit_share_payout_schedule) are computed in the cession mart,
-- because they also leave the reserve there; this statement reads them.
paid as (
    select * from ce
),

stmt as (
    select
        *,
        closing_profit_share_due as closing_due,
        coalesce(lag(itd_profit_share_due) over w, 0) as prior_itd_earned,
        coalesce(lag(itd_distributions_paid) over w, 0) as prior_itd_paid
    from paid
    window w as (partition by participation_entity_key order by posting_period_key)
)

select
    s.participation_entity_key,
    s.entity_id,
    s.entity_name,
    s.entity_type,
    s.dealer_key,
    d.dealer_number,
    d.dealer_name,
    s.tenant_key,
    s.posting_period_key,
    s.period_end,
    case when s.is_closed then 'FINAL' else 'PRELIMINARY' end as statement_status,
    case when s.owns_claims_reserve then 'DEALER_ENTITY_SURPLUS' else 'PAYABLE' end as due_category,
    s.itd_earned_reserve,
    s.itd_paid_claims,
    s.itd_case_reserve,
    s.itd_incurred_claims,
    s.itd_ceding_fee,
    s.itd_underwriting_result,
    s.itd_loss_ratio,
    s.dealer_share_pct,
    s.is_seasoned,
    s.months_seasoned,
    s.itd_profit_share_due as itd_profit_share_earned,
    s.prior_itd_earned - s.prior_itd_paid as opening_profit_share_due,
    s.itd_profit_share_due - s.prior_itd_earned as period_profit_share,
    s.itd_distributions_paid - s.prior_itd_paid as distributions_paid,
    s.itd_distributions_paid,
    s.closing_due as closing_profit_share_due,
    s.owns_claims_reserve,
    s.reserve_balance,
    s.claims_reserve_released,
    s.itd_claims_reserve_released,
    s.itd_profit_share_reserve_funded,
    -- PAYABLE structures only (null for reserve-owning ones, which pay from their own reserve).
    -- ps_reserve_shortfall: the share earned beyond the profit-share reserve funded.
    -- funding_gap: the share earned beyond everything the obligor holds for these contracts, i.e. the
    -- profit-share reserve plus the earned surplus left in the claims reserve (earned - incurred).
    -- Positive = short.
    case
        when not s.owns_claims_reserve then s.itd_profit_share_due - s.itd_profit_share_reserve_funded
    end as ps_reserve_shortfall,
    case
        when not s.owns_claims_reserve
            then s.itd_profit_share_due - s.itd_profit_share_reserve_funded
                - greatest(s.itd_earned_reserve - s.itd_incurred_claims, 0)
    end as funding_gap,
    {{ audit_columns() }}
from stmt as s
left join {{ ref('dim_dealer') }} as d using (dealer_key)
