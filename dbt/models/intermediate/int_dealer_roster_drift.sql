-- Dealer roster drift: the admin-API dealer roster (an independent extract of the admin system's
-- dealer master) against core.dim_dealer. One row per dealer number seen on either side.
--   drift_type  OK                      same dealer, same tenant / state / status / name / remit rule
--               API_RECORD_QUARANTINED  the API record was rejected by the data contract (no accepted
--                                       copy yet); informational, the producer re-sends next day
--               NOT_REPORTABLE          on the roster but a demo / training dealer (int_reportable_dealers)
--               LEGACY_ONLY             in dim_dealer from the legacy book only (the API never had it)
--               MISSING_IN_DIM          on the roster, reportable, but not in dim_dealer       -> gate fails
--               MISSING_IN_API          in dim_dealer (Admin Core) but not on the roster        -> gate fails
--               ATTRIBUTE_MISMATCH      both sides, attributes disagree (mismatched_attributes) -> gate fails
with api as (
    select * from {{ ref('stg_admin_api__dealer') }}
),

quarantined as (
    select distinct upper(trim(raw_json::json ->> 'dealer_number')) as dealer_number
    from {{ source('admin_api', '_quarantine') }}
),

dim as (
    select d.*, t.tenant_code
    from {{ ref('dim_dealer') }} as d
    left join {{ ref('dim_tenant') }} as t on t.tenant_key = d.tenant_key
    where d.dealer_key <> '-1'
),

reportable as (
    select dealer_number, is_reportable from {{ ref('int_reportable_dealers') }}
),

joined as (
    select
        coalesce(a.dealer_number, d.dealer_number) as dealer_number,
        d.dealer_key,
        d.source_system as dim_source_system,
        a.dealer_number is not null as on_api_roster,
        d.dealer_number is not null as in_dim_dealer,
        coalesce(r.is_reportable, true) as is_reportable,
        a.extract_date,
        concat_ws(', ',
            case when a.tenant_code is distinct from d.tenant_code then 'tenant' end,
            case when a.state is distinct from d.state then 'state' end,
            case when a.status is distinct from upper(d.dealer_status) then 'status' end,
            case when a.dealer_name is distinct from d.dealer_name then 'dealer_name' end,
            case when a.remit_rule is distinct from d.remit_rule then 'remit_rule' end
        ) as mismatched_attributes
    from api as a
    full outer join dim as d on d.dealer_number = a.dealer_number
    left join reportable as r on r.dealer_number = coalesce(a.dealer_number, d.dealer_number)
)

select
    j.dealer_number,
    j.dealer_key,
    j.dim_source_system,
    j.on_api_roster,
    j.in_dim_dealer,
    j.is_reportable,
    j.extract_date,
    case when j.on_api_roster and j.in_dim_dealer then nullif(j.mismatched_attributes, '') end as mismatched_attributes,
    case
        when j.on_api_roster and j.in_dim_dealer and nullif(j.mismatched_attributes, '') is null then 'OK'
        when j.on_api_roster and j.in_dim_dealer then 'ATTRIBUTE_MISMATCH'
        when j.on_api_roster and not j.is_reportable then 'NOT_REPORTABLE'
        when j.on_api_roster then 'MISSING_IN_DIM'
        when j.dim_source_system = 'legacy_admin' then 'LEGACY_ONLY'
        when q.dealer_number is not null then 'API_RECORD_QUARANTINED'
        else 'MISSING_IN_API'
    end as drift_type
from joined as j
left join quarantined as q on q.dealer_number = j.dealer_number
