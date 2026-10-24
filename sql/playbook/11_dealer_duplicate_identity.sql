-- pattern:  dealer duplicate-identity lookup (always run before filtering by dealer)
-- question: Which dealers exist under more than one "DealerId", what are the variants (inactive copy,
--           prefixed number, padded name), and how many contracts hang off each id?
-- trap:     filtering by one "DealerId", or by an exact "Dealer Name", silently misses the contracts
--           booked on the other identities. Group on a normalized name AND the digits of the dealer
--           number, list every id with its contract count, and only then pick the ids for the report.

set statement_timeout = '30s';

select "DealerId",
       "Dealer Name",
       "Number",
       "Status",
       "PhysicalCity"
from legacy_ops.vw_dim_dealers
where "Dealer Name" ilike '%juniper harbor%'
order by 1;

with d as (
    select "DealerId",
           "Dealer Name",
           "Number",
           "Status",
           upper(trim("Dealer Name")) as name_key,
           regexp_replace("Number", '[^0-9]', '', 'g') as number_key
    from legacy_ops.vw_dim_dealers
), k as (
    select number_key
    from d
    group by 1
    having count(distinct "DealerId") > 1
), n as (
    select "fxDealerId",
           count(*) as contracts
    from legacy_ops.tbl_contract
    group by 1
)
select d.number_key,
       min(d.name_key) as dealer,
       count(distinct d."DealerId") as identities,
       string_agg(d."DealerId" || ' ' || d."Number" || ' ' || d."Status" || ' (' || coalesce(n.contracts, 0) || ')', ', ' order by d."DealerId") as variants,
       sum(coalesce(n.contracts, 0)) as contracts_all_ids,
       max(n.contracts) filter (where d."Status" = 'ACTIVE' and d."Number" !~ '^[A-Z]{2}') as contracts_main_id_only
from d
join k using (number_key)
left join n on n."fxDealerId" = d."DealerId"
group by 1
order by 1;
