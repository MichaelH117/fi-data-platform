{{ config(severity='warn') }}
-- Sanity check: real VSC claims wait on parts and inspections, so a book with open claims but none
-- aged 30+ is suspicious (usually the source closes every claim too fast). Warns, never blocks.
select sum(open_claims) as open_claims, sum(open_claims_aged_30_plus) as aged_30_plus
from {{ ref('mart_claims_kpi') }}
having sum(open_claims) > 0 and coalesce(sum(open_claims_aged_30_plus), 0) = 0
