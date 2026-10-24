-- pattern:  DAX measure -> SQL translation 1 of 4 (CALCULATE with a filter argument)
-- question: Contracts, In Force and Written Premium by reportable state, exactly as the Ops model's
--           Contracts table shows them, plus the Reporting model's Contracts for the same slice.
-- source:   legacy/semantic_models/Ops.SemanticModel, table Contracts (bound to legacy_ops.vw_dim_contracts)
--             Contracts         = COUNTROWS(Contracts)
--             'Written Premium' = SUM(Contracts[ContractPurchasePrice])
--             'In Force'        = CALCULATE([Contracts], Contracts[CurrentContractStatus] = "INFORCE")
--           legacy/semantic_models/Reporting.SemanticModel, table Fact Contracts
--             Contracts         = DISTINCTCOUNT('Fact Contracts'[fxContractId])
-- translate: a visual's row axis is the group by; COUNTROWS is count(*); CALCULATE(measure, col = x)
--           replaces the filter on that column, which is count(*) filter (where col = x) in the same group;
--           DISTINCTCOUNT is count(distinct ...).
-- trap:     both models call their measure "Contracts" but they count different tables. The Ops count
--           includes demo dealers and API test contracts, and "In Force" is today's status, not an as-of
--           rule, so neither is a filing number.

set statement_timeout = '60s';

with r as (
    select c."Reportable State",
           count(distinct f."fxContractId") as reporting_contracts
    from legacy_reporting.vw_fact_contracts f
    join legacy_ops.tbl_contract c on c."ContractId" = f."fxContractId"
    group by 1
)
select o."Reportable State",
       count(*) as "Contracts",
       count(*) filter (where o."CurrentContractStatus" = 'INFORCE') as "In Force",
       sum(o."ContractPurchasePrice") as "Written Premium",
       max(r.reporting_contracts) as "Reporting Contracts"
from legacy_ops.vw_dim_contracts o
left join r using ("Reportable State")
group by 1
order by 1;
