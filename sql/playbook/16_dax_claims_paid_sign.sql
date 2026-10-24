-- pattern:  DAX measure -> SQL translation 2 of 4 (same measure name, opposite sign)
-- question: Claims Paid by paid year in the Ops model and in the Reporting model.
-- source:   legacy/semantic_models/Ops.SemanticModel, table Claims (legacy_ops.vw_dim_claims)
--             'Claims Paid' = SUM(Claims[ClaimAmount])
--           legacy/semantic_models/Reporting.SemanticModel, table Fact Claims (legacy_reporting.vw_fact_claims)
--             'Claims Paid' = SUM('Fact Claims'[Claim Amount])
-- translate: SUM(table[col]) is sum(col) at the grain of the visual. The Ops table has a paid date; the
--           Reporting fact only has a created date, so the Reporting year axis is the created year.
-- trap:     the Ops layer stores the amount NEGATIVE and the Reporting layer POSITIVE, so the two "Claims
--           Paid" cards on two reports disagree in sign, and a net (premium - claims) built on the Ops
--           measure adds the claims back instead of taking them out. Neither measure filters on status.

set statement_timeout = '60s';

with o as (
    select extract(year from "PaidDate")::int as yr,
           sum("ClaimAmount") as ops_claims_paid
    from legacy_ops.vw_dim_claims
    group by 1
), r as (
    select extract(year from "Created Date")::int as yr,
           sum("Claim Amount") as reporting_claims_paid
    from legacy_reporting.vw_fact_claims
    group by 1
)
select yr,
       o.ops_claims_paid,
       r.reporting_claims_paid,
       o.ops_claims_paid + r.reporting_claims_paid as ops_plus_reporting
from o
full join r using (yr)
order by 1;
