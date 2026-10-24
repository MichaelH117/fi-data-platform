-- pattern:  DAX measure -> SQL translation 2 of 4 (modern star schema)
-- question: Claims Paid by paid year from the one certified claim fact.
-- trap:     none expected. One fact, one definition, paid_amount positive (is_amount_sign_fixed records the
--           rows the pipeline corrected), so every report that sums it agrees.

set statement_timeout = '60s';

select extract(year from paid_date)::int as yr,
       sum(paid_amount) as claims_paid,
       count(*) filter (where is_amount_sign_fixed) as sign_fixed_rows
from gold.fct_claim
where paid_date is not null
group by 1
order by 1;
