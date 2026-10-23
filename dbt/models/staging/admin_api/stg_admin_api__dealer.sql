-- Admin system REST API dealer roster: typed, minus quarantined records and incomplete extracts,
-- one row per dealer_number from the newest accepted extract. This is the independent roster
-- dim_dealer is checked against (int_dealer_roster_drift, reconciliation gate test recon_dealer_roster_drift).
with accepted as (
    {{ intake_accepted('admin_api', 'dealer') }}
),

latest as (
    select *
    from accepted
    qualify row_number() over (
        partition by upper(trim(dealer_number))
        order by _feed_date desc, try_cast(updated_at as timestamp) desc nulls last, _source_file desc
    ) = 1
)

select
    {{ clean_code('dealer_number') }} as dealer_number,
    trim(dealer_name) as dealer_name,
    upper(trim(tenant_code)) as tenant_code,
    upper(trim(state)) as state,
    upper(trim(status)) as status,
    nullif(upper(trim(remit_rule)), '') as remit_rule,
    try_cast(updated_at as timestamp) as updated_at,
    _feed_date as extract_date,
    _source_file,
    _row_number,
    _source_system,
    _loaded_at
from latest
