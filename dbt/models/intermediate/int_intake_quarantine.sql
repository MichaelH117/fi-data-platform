{{ config(materialized='table') }}
-- Every row an intake control rejected, all sources, one vocabulary: dealer-integration network
-- feed (_quarantine; Admin Core logs the same rejects in intake_reject), CRM export, admin-API
-- roster pages and the legacy admin contract check. reject_key is the row's business key from its
-- raw JSON, so a reject can be followed to its accepted copy.
with feed as (
    select
        'network_feed' as source_system,
        replace(replace(file_name, '.csv', ''), '.jsonl', '') as source_table,
        reject_reason,
        case replace(replace(file_name, '.csv', ''), '.jsonl', '')
            when 'deals' then raw_json::json ->> 'deal_id'
            when 'econtract_events' then raw_json::json ->> 'event_id'
            when 'remittance' then (raw_json::json ->> 'remit_batch_id') || ' / '
                                   || upper(trim(raw_json::json ->> 'contract_number'))
        end as reject_key,
        _feed_date as feed_date,
        _source_file as source_file,
        _row_number as source_row_number,
        _loaded_at
    from {{ source('network_feed', '_quarantine') }}
),

crm as (
    select 'crm', 'calls', reject_reason, trim(raw_json::json ->> 'call_id'), _feed_date, _source_file,
           _row_number, _loaded_at
    from {{ source('crm', '_quarantine') }}
),

admin_api as (
    select 'admin_api', 'dealer', reject_reason, upper(trim(raw_json::json ->> 'dealer_number')), _feed_date,
           _source_file, _row_number, _loaded_at
    from {{ source('admin_api', '_quarantine') }}
),

legacy as (
    select 'legacy_admin', table_name, reject_reason,
           coalesce(raw_json::json ->> 'legacy_contract_id', raw_json::json ->> 'claim_no',
                    raw_json::json ->> 'cancellation_id'),
           cast(_loaded_at as date), cast(null as varchar), cast(null as bigint), _loaded_at
    from {{ source('legacy_admin', '_quarantine') }}
),

rejects as (
    select * from feed
    union all select * from crm
    union all select * from admin_api
    union all select * from legacy
),

-- accepted copies of each key (staging keeps only rows no control rejected)
accepted as (
    select 'network_feed' as source_system, 'deals' as source_table, deal_id as accepted_key,
           _source_file, _row_number from {{ ref('stg_network_feed__deals') }}
    union all
    select 'network_feed', 'econtract_events', event_id, _source_file, _row_number
    from {{ ref('stg_network_feed__econtract_events') }}
    union all
    select 'network_feed', 'remittance', remit_batch_id || ' / ' || contract_number, _source_file, _row_number
    from {{ ref('stg_network_feed__remittance') }}
    union all
    select 'crm', 'calls', call_id, _source_file, _row_number from {{ ref('stg_crm__calls') }}
    union all
    select 'admin_api', 'dealer', dealer_number, _source_file, _row_number from {{ ref('stg_admin_api__dealer') }}
    union all
    select 'legacy_admin', 'contracts', cast(legacy_contract_id as varchar), null, null
    from {{ ref('stg_legacy_admin__contracts') }}
    where is_recovered_from_quarantine
),

admin_core_log as (
    select feed_date, file_name, row_number from {{ ref('stg_admin_core__intake_reject') }} where not is_deleted
)

select
    {{ surrogate_key(['r.source_system', 'r.source_table', 'r.source_file', 'r.source_row_number',
                      'r.reject_reason', 'r.reject_key']) }} as reject_row_key,
    r.source_system,
    r.source_table,
    r.reject_reason,
    -- 'bad_pattern:vin;duplicate_call_id' -> 'bad_pattern' (the control family)
    split_part(split_part(r.reject_reason, ';', 1), ':', 1) as reject_control,
    r.reject_key,
    r.feed_date,
    r.source_file,
    r.source_row_number,
    case
        when r.source_system = 'legacy_admin' and a.accepted_key is not null then 'recovered'
        when a.accepted_key is not null and r.reject_reason like '%duplicate%' then 'duplicate_dropped'
        when a.accepted_key is not null then 'replayed'
        else 'open'
    end as resolution_status,
    a._source_file as resolved_by_file,
    r.source_system = 'network_feed' and l.row_number is not null as is_logged_in_admin_core,
    r._loaded_at
from rejects as r
left join accepted as a
    on a.source_system = r.source_system and a.source_table = r.source_table and a.accepted_key = r.reject_key
left join admin_core_log as l
    on r.source_system = 'network_feed' and l.feed_date = r.feed_date
   and r.source_file like '%' || l.file_name and l.row_number = r.source_row_number
