-- Migration parallel run: the same business objects counted in BOTH systems at the cutover
-- (var cutover_date), with row and dollar differences and the documented exclusions that explain
-- them. Grain: entity x cutover posting period. Test: recon_migration_parallel_run_ties.
--
--   CLAIM_OPEN_AT_CUTOVER     legacy claims still open on the day before cutover vs the Admin Core
--                             claims converted from them (same normalized claim number); $ = requested.
--                             Exclusion DEDUCTIBLE_NETTING: Admin Core stores some converted
--                             requests net of the deductible, legacy stored them gross.
--   CLAIM_CONTRACT_LINK       the same converted claims: legacy contract vs Admin Core
--                             legacy_contract_ref. Exclusion MISSING_CONTRACT_REF: converted claims
--                             entered without a contract reference (claim bridge has_contract_key).
--   CONTRACT_IN_FORCE_AT_CUTOVER  the legacy in-force book on the day before cutover vs Admin Core
--                             contracts converted from it; $ = coverage cost. The legacy book is not
--                             converted yet (it stays on the legacy system of record), so status is
--                             NOT_MIGRATED until the source converts it.
-- status: TIED (no difference), TIED_AFTER_EXCLUSIONS, NOT_MIGRATED, BREAK.
with cut as (
    select cutover_date, cast(cutover_date - interval 1 day as date) as last_legacy_day
    from {{ ref('int_as_of_date') }}
),

conv as (
    select claim_number, admin_claim_id, has_contract_key
    from {{ ref('int_claim_number_bridge') }}
    where is_converted
),

legacy_open as (
    select l.claim_number, l.legacy_contract_id, l.amount_requested
    from {{ ref('stg_legacy_admin__claims') }} as l
    cross join cut
    where not l.is_deleted
      and l.claim_status = 'open'
      and l.reported_date <= cut.last_legacy_day
),

claim_pairs as (
    select
        lo.claim_number,
        lo.legacy_contract_id,
        lo.amount_requested as legacy_amount,
        c.claim_id as core_claim_id,
        c.requested_amount as core_amount,
        c.deductible_amount,
        c.legacy_contract_ref,
        cv.has_contract_key
    from legacy_open as lo
    left join conv as cv on cv.claim_number = {{ normalize_claim_number('lo.claim_number') }}
    left join {{ ref('stg_admin_core__claim') }} as c on c.claim_id = cv.admin_claim_id and not c.is_deleted
),

claims_open as (
    select
        'CLAIM_OPEN_AT_CUTOVER' as entity,
        count(*) as legacy_rows,
        coalesce(sum(legacy_amount), 0) as legacy_amount,
        count(core_claim_id) as admin_core_rows,
        coalesce(sum(core_amount), 0) as admin_core_amount,
        0 as excluded_rows,
        coalesce(sum(case when abs(legacy_amount - core_amount - deductible_amount) <= 0.01
                          and abs(legacy_amount - core_amount) > 0.01 then deductible_amount end), 0)
            as excluded_amount,
        'DEDUCTIBLE_NETTING' as exclusion_code
    from claim_pairs
),

claims_link as (
    select
        'CLAIM_CONTRACT_LINK' as entity,
        count(*) as legacy_rows,
        0.0 as legacy_amount,
        count(*) filter (where legacy_contract_ref = legacy_contract_id) as admin_core_rows,
        0.0 as admin_core_amount,
        count(*) filter (where core_claim_id is not null and legacy_contract_ref is null) as excluded_rows,
        0.0 as excluded_amount,
        'MISSING_CONTRACT_REF' as exclusion_code
    from claim_pairs
),

legacy_book as (
    select c.contract_key, c.coverage_cost
    from {{ ref('int_contracts_conformed') }} as c
    cross join cut
    left join {{ ref('int_cancellations_conformed') }} as x
        on x.contract_key = c.contract_key and x.is_effective_cancel
    where c.source_system = 'legacy_admin'
      and c.effective_date <= cut.last_legacy_day
      and c.expiration_date >= cut.cutover_date
      and (x.cancel_date is null or x.cancel_date >= cut.cutover_date)
),

contracts as (
    select
        'CONTRACT_IN_FORCE_AT_CUTOVER' as entity,
        count(*) as legacy_rows,
        coalesce(sum(coverage_cost), 0) as legacy_amount,
        -- Admin Core has no converted contract rows: no contract carries a legacy reference.
        0 as admin_core_rows,
        0.0 as admin_core_amount,
        0 as excluded_rows,
        0.0 as excluded_amount,
        'NOT_CONVERTED' as exclusion_code
    from legacy_book
),

unioned as (
    select * from claims_open
    union all select * from claims_link
    union all select * from contracts
)

select
    u.entity,
    {{ yyyymm('cut.cutover_date') }} as posting_period_key,
    {{ period_start_date(yyyymm('cut.cutover_date')) }} as posting_period_start_date,
    cut.last_legacy_day as legacy_as_of_date,
    cut.cutover_date,
    u.legacy_rows,
    u.admin_core_rows,
    u.legacy_rows - u.admin_core_rows as row_difference,
    round(u.legacy_amount, 2) as legacy_amount,
    round(u.admin_core_amount, 2) as admin_core_amount,
    round(u.legacy_amount - u.admin_core_amount, 2) as amount_difference,
    u.exclusion_code,
    u.excluded_rows,
    round(u.excluded_amount, 2) as excluded_amount,
    u.legacy_rows - u.admin_core_rows - u.excluded_rows as unexplained_rows,
    round(u.legacy_amount - u.admin_core_amount - u.excluded_amount, 2) as unexplained_amount,
    case
        when u.entity = 'CONTRACT_IN_FORCE_AT_CUTOVER' and u.admin_core_rows = 0 then 'NOT_MIGRATED'
        when u.legacy_rows = u.admin_core_rows and abs(u.legacy_amount - u.admin_core_amount) <= 0.01 then 'TIED'
        when u.legacy_rows - u.admin_core_rows - u.excluded_rows = 0
             and abs(u.legacy_amount - u.admin_core_amount - u.excluded_amount) <= 0.01 then 'TIED_AFTER_EXCLUSIONS'
        else 'BREAK'
    end as status,
    {{ audit_columns() }}
from unioned as u
cross join cut
