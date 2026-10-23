{{ config(materialized='table') }}
-- Accounting transactions on the coverage-cost (dealer-cost) basis, with the posting period
-- assigned by the period-lock rule (macro posting_period_key, int_posting_periods).
-- Admin Core: every transaction (SALE, CANCEL, REINSTATE, AMEND, CASH). Legacy has no ledger, so
-- the conversion books one SALE per contract (dealer cost, on the sale date) and one CANCEL per
-- cancellation (minus the coverage-cost refund, on the cancel date).
with core as (
    select
        md5('admin_core|txn|' || cast(t.txn_id as varchar)) as txn_key,
        'admin_core' as source_system,
        cast(t.txn_id as varchar) as source_txn_id,
        {{ contract_key("'admin_core'", 't.contract_id') }} as contract_key,
        t.txn_type,
        t.posted_at,
        t.created_at as recorded_at,
        t.amount,
        t.source_ref
    from {{ ref('stg_admin_core__transaction') }} as t
    where not t.is_deleted
),

legacy_sales as (
    select
        md5('legacy_admin|txn|SALE|' || source_contract_id) as txn_key,
        'legacy_admin' as source_system,
        'SALE-' || source_contract_id as source_txn_id,
        contract_key,
        'SALE' as txn_type,
        cast(entered_date as timestamp) as posted_at,
        cast(entered_date as timestamp) as recorded_at,
        coverage_cost as amount,
        contract_number as source_ref
    from {{ ref('int_contracts_conformed') }}
    where source_system = 'legacy_admin'
),

legacy_cancels as (
    select
        md5('legacy_admin|txn|CANCEL|' || source_cancel_id) as txn_key,
        'legacy_admin' as source_system,
        'CANCEL-' || source_cancel_id as source_txn_id,
        contract_key,
        'CANCEL' as txn_type,
        cast(cancel_date as timestamp) as posted_at,
        coalesce(recorded_at, cast(cancel_date as timestamp)) as recorded_at,
        -coverage_cost_refund as amount,
        source_cancel_id as source_ref
    from {{ ref('int_cancellations_conformed') }}
    where source_system = 'legacy_admin'
),

unioned as (
    select * from core
    union all
    select * from legacy_sales
    union all
    select * from legacy_cancels
)

select
    u.*,
    {{ posting_period_key('u.posted_at', 'pp') }} as posting_period_key
from unioned as u
{{ posting_period_join('u.recorded_at', 'pp') }}
