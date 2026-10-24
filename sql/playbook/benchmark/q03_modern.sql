-- database: modern
-- timeout: 60s
-- question: Paid claims over the last 24 months on the modern claim fact.
-- fix:      the sign convention is fixed once in staging (paid amounts are positive everywhere, with a test).
set statement_timeout = '60s';

select round(sum(paid_amount)::numeric, 2) as paid_claims, count(distinct date_trunc('month', paid_date)) as months
from gold.fct_claim
where paid_date >= date '2024-11-01';
