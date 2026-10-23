-- Every counted FUNDED_RESERVE line uses the same product scope, so Section 2 and Section 3
-- describe the same book. Each line also names a status set when its measure needs one.
select report_id, count(distinct coalesce(category_filter, '') || '/' || coalesce(subcategory_exclude, '')) as scopes
from {{ ref('report_definitions') }}
where measure <> 'DERIVED'
group by 1
having count(distinct coalesce(category_filter, '') || '/' || coalesce(subcategory_exclude, '')) > 1

union all

select report_id || ':' || line_item, 0
from {{ ref('report_definitions') }}
where measure not in ('DERIVED', 'COMPLAINT_COUNT') and status_set_id is null
