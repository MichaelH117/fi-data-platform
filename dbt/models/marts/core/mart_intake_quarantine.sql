-- Grain: one rejected intake row (all sources). What the intake controls caught, why, and whether
-- each reject was replayed (its key later accepted from a corrected or re-sent file), dropped as a
-- duplicate of an accepted copy, recovered (legacy MM/DD/YYYY dates) or is still open. The data
-- quality page reads this mart instead of raw tables.
select
    reject_row_key,
    source_system,
    source_table,
    reject_reason,
    reject_control,
    reject_key,
    feed_date,
    source_file,
    source_row_number,
    resolution_status,
    resolution_status <> 'open' as is_resolved,
    resolved_by_file,
    is_logged_in_admin_core,
    {{ audit_columns() }}
from {{ ref('int_intake_quarantine') }}
