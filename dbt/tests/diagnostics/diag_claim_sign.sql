-- Diagnostic: claim sign. After conforming, paid_amount is never negative. The raw legacy feed
-- stores claim money negative by design; if it ever arrives positive the documented contract has
-- changed upstream and staging's sign flip must be reviewed, so that fails too.
select 'fct_claim_payment_line paid < 0' as check_name, count(*) as bad_rows
from {{ ref('fct_claim_payment_line') }}
where paid_amount < 0
having count(*) > 0

union all

select 'int_claim_lines_conformed paid < 0', count(*)
from {{ ref('int_claim_lines_conformed') }}
where paid_amount < 0
having count(*) > 0

union all

select 'raw legacy claims stored positive', count(*)
from {{ source('legacy_admin', 'claims') }}
where amount_paid > 0 or amount_requested > 0
having count(*) > 0
