-- The cession mart and the profit-share statement report the same closing profit share due per
-- entity (earned ITD - distributions paid ITD) at the latest closed period, and the same paid ITD.
with closed as (
    select max(posting_period_key) as k from {{ ref('mart_cession_experience') }} where is_closed
),

ce as (
    select participation_entity_key, closing_profit_share_due as due, itd_distributions_paid as paid
    from {{ ref('mart_cession_experience') }}
    where posting_period_key = (select k from closed)
),

st as (
    select participation_entity_key, closing_profit_share_due as due, itd_distributions_paid as paid
    from {{ ref('mart_profit_share_statement') }}
    where posting_period_key = (select k from closed)
)

select participation_entity_key, ce.due as cession_due, st.due as statement_due
from ce
full outer join st using (participation_entity_key)
where abs(coalesce(ce.due, 0) - coalesce(st.due, 0)) > 0.005
    or abs(coalesce(ce.paid, 0) - coalesce(st.paid, 0)) > 0.005
union all
select null, sum(ce.due), (select sum(due) from st)
from ce
having abs(sum(ce.due) - (select sum(due) from st)) > 0.005
