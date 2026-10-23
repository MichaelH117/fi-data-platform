-- Cutover sign-off check: every migrated entity in the parallel run ties between the legacy
-- system and Admin Core, by rows and dollars, after its documented exclusion. NOT_MIGRATED
-- entities are reported in the mart (and by pipeline.reconcile) but are not a break here.
select entity, unexplained_rows, unexplained_amount, status
from {{ ref('mart_migration_parallel_run') }}
where status = 'BREAK'
   or (status <> 'NOT_MIGRATED' and (unexplained_rows <> 0 or abs(unexplained_amount) > 0.01))
