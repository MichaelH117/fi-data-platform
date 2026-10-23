-- States with no funded-reserve requirement are skipped; every requirement state with in-scope
-- contracts is filed.
select s.state, 'filed_without_requirement' as problem
from {{ ref('mart_funded_reserve_summary') }} as s
inner join {{ ref('state_reserve_method') }} as m using (state)
where not m.has_requirement

union all

select distinct c.state, 'requirement_state_missing'
from {{ ref('mart_funded_reserve_contracts') }} as c
where c.has_requirement and c.is_filing_product_scope and c.is_written_itd
  and c.state not in (select state from {{ ref('mart_funded_reserve_summary') }})
