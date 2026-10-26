-- P&L by product. Grain: tenant x product (category > subcategory > product code) x posting period.
-- Written side: coverage cost written (fct_gross_premium) and coverage-cost refunds (fct_cancellation).
-- Earned side: each cost-split bucket's earned amount (fct_earned_disbursement), grouped into P&L
-- lines by the seed pnl_line_map. Claims from fct_claim_payment_line.
-- Written side is the posted ledger, period by period: refunds on every cancel posted (count and
-- dollars on the same basis) and the add-back of approved reinstatements (fct_reinstatement) in the
-- period they post, so net written = written - refunded + reinstated ties to fct_transaction
-- SALE + AMEND + CANCEL + REINSTATE.
-- Claims with no contract key sit on the dim_product '-1' member. They are not a product: they get
-- product_code 'UNMATCHED' and is_unmatched_claims = true, so a report shows them as one reconciling
-- line below the product total (the grand total still ties to fct_claim_payment_line).
-- posted_loss_ratio = incurred / earned claims reserve (metrics.yml posted_loss_ratio);
-- loss_ratio = incurred / earned premium (metrics.yml loss_ratio).
-- dealer_participation = the profit share the statement books for the dealer entities this period
-- (mart_profit_share_statement.period_profit_share), spread over each entity's products by its
-- ITD earned claims reserve; administrator_result = admin fee + program_margin_after_participation
-- is what the administrator keeps.
with prod as (
    select
        product_key,
        product_key::varchar = '-1' as is_unmatched_claims,
        case when product_key::varchar = '-1' then 'UNMATCHED' else category_code end as category_code,
        case when product_key::varchar = '-1' then 'Unmatched claims' else category end as category,
        case when product_key::varchar = '-1' then 'UNMATCHED' else subcategory_code end as subcategory_code,
        case when product_key::varchar = '-1' then 'No contract key' else subcategory end as subcategory,
        case when product_key::varchar = '-1' then 'UNMATCHED' else product_code end as product_code
    from {{ ref('dim_product') }}
),

pmap as (
    select disbursement_type, pnl_group from {{ ref('pnl_line_map') }}
),

written as (
    select g.tenant_key, p.product_code, g.posting_period_key,
        sum(g.contracts_written) as contracts_written,
        sum(g.coverage_cost) as written_coverage_cost,
        sum(g.retail_price) as written_retail
    from {{ ref('fct_gross_premium') }} as g
    inner join prod as p using (product_key)
    group by 1, 2, 3
),

cancels as (
    select x.tenant_key, p.product_code, x.posting_period_key,
        count(*) as contracts_cancelled,
        sum(x.coverage_cost_refund) as coverage_cost_refunded,
        sum(x.customer_refund) as customer_refunds
    from {{ ref('fct_cancellation') }} as x
    inner join prod as p using (product_key)
    group by 1, 2, 3
),

earned as (
    select e.tenant_key, p.product_code, e.posting_period_key,
        sum(e.earned_amount) as earned_premium,
        sum(e.earned_amount) filter (where m.pnl_group = 'RESERVE') as earned_claims_reserve,
        sum(e.earned_amount) filter (where m.pnl_group = 'ADMIN') as earned_admin_fee,
        sum(e.earned_amount) filter (where m.pnl_group = 'ACQUISITION') as earned_acquisition_cost,
        sum(e.earned_amount) filter (where m.pnl_group = 'PROFIT_SHARE') as earned_profit_share_reserve,
        sum(e.earned_amount) filter (where e.disbursement_type = 'COMMISSION') as earned_commission,
        sum(e.earned_amount) filter (where e.disbursement_type = 'INSURER_FEE') as earned_insurer_fee,
        sum(e.earned_amount) filter (where e.disbursement_type = 'PREMIUM_TAX') as earned_premium_tax
    from {{ ref('fct_earned_disbursement') }} as e
    inner join prod as p using (product_key)
    left join pmap as m using (disbursement_type)
    group by 1, 2, 3
),

reinstated as (
    select r.tenant_key, p.product_code, r.posting_period_key,
        count(*) as contracts_reinstated,
        sum(r.reversal_amount) as coverage_cost_reinstated
    from {{ ref('fct_reinstatement') }} as r
    inner join prod as p using (product_key)
    where r.status = 'APPROVED'
    group by 1, 2, 3
),

claims as (
    select l.tenant_key, p.product_code, l.posting_period_key,
        count(*) as claim_lines,
        sum(l.paid_amount) as paid_claims,
        sum(l.incurred_change) as incurred_claims
    from {{ ref('fct_claim_payment_line') }} as l
    inner join prod as p using (product_key)
    group by 1, 2, 3
),

-- Dealer participation: each entity's period profit share, spread over its products by ITD earned
-- claims reserve (by contracts written while an entity has earned nothing yet).
entity_contract as (
    select contract_key, participation_entity_key  -- ceded contracts only (run-off rule)
    from {{ ref('mart_participation_contract') }}
    where is_ceded
),

entity_earned as (
    select ec.participation_entity_key, p.product_code, sum(e.earned_reserve) as earned_reserve
    from {{ ref('fct_earned_disbursement') }} as e
    inner join entity_contract as ec using (contract_key)
    inner join prod as p using (product_key)
    where e.disbursement_type = 'CLAIMS_RESERVE'
    group by 1, 2
),

entity_written as (
    select ec.participation_entity_key, p.product_code, sum(g.contracts_written) as contracts_written
    from {{ ref('fct_gross_premium') }} as g
    inner join entity_contract as ec using (contract_key)
    inner join prod as p using (product_key)
    group by 1, 2
),

entity_weight as (
    select
        participation_entity_key,
        product_code,
        coalesce(
            coalesce(ee.earned_reserve, 0) / nullif(sum(coalesce(ee.earned_reserve, 0)) over e, 0),
            coalesce(ew.contracts_written, 0) / nullif(sum(coalesce(ew.contracts_written, 0)) over e, 0)
        ) as weight
    from entity_earned as ee
    full outer join entity_written as ew using (participation_entity_key, product_code)
    window e as (partition by participation_entity_key)
),

participation as (
    select s.tenant_key, w.product_code, s.posting_period_key,
        sum(s.period_profit_share * w.weight) as dealer_participation
    from {{ ref('mart_profit_share_statement') }} as s
    inner join entity_weight as w using (participation_entity_key)
    where s.period_profit_share <> 0
    group by 1, 2, 3
),

keys as (
    select tenant_key, product_code, posting_period_key from written
    union select tenant_key, product_code, posting_period_key from cancels
    union select tenant_key, product_code, posting_period_key from reinstated
    union select tenant_key, product_code, posting_period_key from earned
    union select tenant_key, product_code, posting_period_key from claims
    union select tenant_key, product_code, posting_period_key from participation
),

product_names as (
    select product_code, bool_or(is_unmatched_claims) as is_unmatched_claims,
        min(category_code) as category_code, min(category) as category,
        min(subcategory_code) as subcategory_code, min(subcategory) as subcategory
    from prod
    group by 1
),

joined as (
    select
        k.tenant_key,
        coalesce(n.is_unmatched_claims, false) as is_unmatched_claims,
        n.category_code,
        n.category,
        n.subcategory_code,
        n.subcategory,
        k.product_code,
        k.posting_period_key,
        coalesce(w.contracts_written, 0) as contracts_written,
        coalesce(c.contracts_cancelled, 0) as contracts_cancelled,
        coalesce(r.contracts_reinstated, 0) as contracts_reinstated,
        coalesce(w.written_coverage_cost, 0) as written_coverage_cost,
        coalesce(w.written_retail, 0) as written_retail,
        coalesce(c.coverage_cost_refunded, 0) as coverage_cost_refunded,
        coalesce(r.coverage_cost_reinstated, 0) as coverage_cost_reinstated,
        coalesce(c.customer_refunds, 0) as customer_refunds,
        coalesce(e.earned_premium, 0) as earned_premium,
        coalesce(e.earned_claims_reserve, 0) as earned_claims_reserve,
        coalesce(e.earned_admin_fee, 0) as earned_admin_fee,
        coalesce(e.earned_acquisition_cost, 0) as earned_acquisition_cost,
        coalesce(e.earned_commission, 0) as earned_commission,
        coalesce(e.earned_insurer_fee, 0) as earned_insurer_fee,
        coalesce(e.earned_premium_tax, 0) as earned_premium_tax,
        coalesce(e.earned_profit_share_reserve, 0) as earned_profit_share_reserve,
        coalesce(cl.claim_lines, 0) as claim_lines,
        coalesce(cl.paid_claims, 0) as paid_claims,
        coalesce(cl.incurred_claims, 0) as incurred_claims,
        coalesce(pt.dealer_participation, 0) as dealer_participation
    from keys as k
    left join product_names as n using (product_code)
    left join written as w using (tenant_key, product_code, posting_period_key)
    left join cancels as c using (tenant_key, product_code, posting_period_key)
    left join reinstated as r using (tenant_key, product_code, posting_period_key)
    left join earned as e using (tenant_key, product_code, posting_period_key)
    left join claims as cl using (tenant_key, product_code, posting_period_key)
    left join participation as pt using (tenant_key, product_code, posting_period_key)
)

select
    *,
    written_coverage_cost - coverage_cost_refunded + coverage_cost_reinstated as net_written_coverage_cost,
    earned_claims_reserve - incurred_claims as reserve_margin,
    -- One treatment of the admin fee everywhere: it is a cost of the program (paid to the
    -- administrator), the same as in combined_ratio. So program_margin = what is left of earned
    -- premium after claims, acquisition costs and the admin fee (= reserve margin + profit-share
    -- reserve), and program_margin / earned_premium = 1 - combined_ratio
    -- (test fin_pnl_margin_matches_combined_ratio).
    earned_premium - incurred_claims - earned_acquisition_cost - earned_admin_fee as program_margin,
    earned_premium - incurred_claims - earned_acquisition_cost - earned_admin_fee - dealer_participation
        as program_margin_after_participation,
    -- What the administrator keeps: its admin fee plus the program margin after dealer participation.
    earned_premium - incurred_claims - earned_acquisition_cost - dealer_participation as administrator_result,
    {{ safe_divide('incurred_claims', 'earned_claims_reserve') }} as posted_loss_ratio,
    {{ safe_divide('incurred_claims', 'earned_premium') }} as loss_ratio,
    {{ safe_divide('incurred_claims + earned_acquisition_cost + earned_admin_fee', 'earned_premium') }}
        as combined_ratio,
    {{ audit_columns() }}
from joined
