-- The state method matrix covers all 50 states + DC exactly once, and every state with a
-- requirement has a method, a reserve rate in (0, 1] and a default basis. Returns failing rows.
with m as (select * from {{ ref('state_reserve_method') }})

select 'row_count' as problem, cast(count(*) as varchar) as detail
from m
having count(*) <> 51 or count(distinct state) <> 51

union all

select 'incomplete_requirement', state
from m
where has_requirement
  and (method_code = 'NONE' or reserve_rate is null or reserve_rate <= 0 or reserve_rate > 1
       or default_basis is null or security_deposit_rate is null)

union all

select 'rate_without_requirement', state
from m
where not has_requirement and (method_code <> 'NONE' or reserve_rate is not null)
