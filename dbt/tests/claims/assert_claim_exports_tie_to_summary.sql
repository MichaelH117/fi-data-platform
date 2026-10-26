-- V6 edit 18: the row-level exports add up to the claim header (mart_claim_summary), per claim.
-- Detail lines and payees carry the same paid and incurred amounts; every claim has a component line.
with detail as (
    select claim_key, round(sum(paid_amount), 2) as paid, round(sum(incurred_change), 2) as incurred,
           count(*) as lines
    from {{ ref('mart_claim_payment_lines') }}
    group by 1
),

payees as (
    select claim_key, round(sum(paid_amount), 2) as paid, round(sum(incurred_amount), 2) as incurred
    from {{ ref('mart_claim_payees') }}
    group by 1
),

components as (
    select claim_key, count(*) as component_lines
    from {{ ref('mart_claim_component_lines') }}
    group by 1
)

select s.claim_key, s.paid_amount, s.incurred_amount, d.paid as detail_paid, p.paid as payee_paid,
       s.payment_lines, d.lines as detail_lines, c.component_lines
from {{ ref('mart_claim_summary') }} as s
left join detail as d using (claim_key)
left join payees as p using (claim_key)
left join components as c using (claim_key)
where abs(s.paid_amount - coalesce(d.paid, 0)) > 0.01
   or abs(s.incurred_amount - coalesce(d.incurred, 0)) > 0.01
   or abs(s.paid_amount - coalesce(p.paid, 0)) > 0.01
   or abs(s.incurred_amount - coalesce(p.incurred, 0)) > 0.01
   or s.payment_lines <> coalesce(d.lines, 0)
   or c.component_lines is null
