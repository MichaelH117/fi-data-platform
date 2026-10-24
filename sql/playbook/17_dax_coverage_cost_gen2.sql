-- pattern:  DAX measure -> SQL translation 3 of 4 (two generations of one measure)
-- question: Coverage cost by entered year in the Finance model, current fact vs the Gen2 copy.
-- source:   legacy/semantic_models/Finance.SemanticModel
--             table Fact Contracts (legacy_finance.vw_fact_contracts)
--               'Coverage Cost'      = SUM('Fact Contracts'[CoverageCost])
--               Contracts            = DISTINCTCOUNT('Fact Contracts'[fxContractId])
--             table Fact Contracts Gen2 (legacy_finance.vw_fact_contracts_gen2)
--               'Coverage Cost Gen2' = SUM('Fact Contracts Gen2'[CoverageCost])
-- translate: two measures on two unrelated tables only line up on a shared axis, so each is aggregated
--           to entered year on its own and then joined on the year (never join the facts row to row).
-- trap:     Gen2 stopped loading weeks ago (the newest contracts are missing) and joins disbursements to
--           the NON-deduped transactions, so the years it does have are inflated. Same model, same
--           "Coverage Cost" word, two answers.

set statement_timeout = '60s';

explain select extract(year from "EnteredDate")::int, sum("CoverageCost")
from legacy_finance.vw_fact_contracts_gen2
group by 1;

with a as (
    select extract(year from "EnteredDate")::int as yr,
           sum("CoverageCost") as coverage_cost,
           count(distinct "fxContractId") as contracts
    from legacy_finance.vw_fact_contracts
    group by 1
), b as (
    select extract(year from "EnteredDate")::int as yr,
           sum("CoverageCost") as coverage_cost_gen2,
           count(distinct "fxContractId") as contracts_gen2
    from legacy_finance.vw_fact_contracts_gen2
    group by 1
)
select yr,
       a.contracts,
       b.contracts_gen2,
       a.coverage_cost,
       b.coverage_cost_gen2,
       b.coverage_cost_gen2 - a.coverage_cost as gen2_minus_current
from a
full join b using (yr)
order by 1;
