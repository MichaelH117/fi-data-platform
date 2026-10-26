-- V7 edit 41: every cancel reason a source sends maps to a reason group in
-- seeds/core/cancel_reason_crosswalk.csv. A new or misspelled reason fails here (add a crosswalk
-- row, never a CASE in a model), so an admin correction is never counted as a customer cancel.
select source_system, reason, count(*) as cancels
from {{ ref('int_cancellations_conformed') }}
where not is_reason_mapped
group by source_system, reason
