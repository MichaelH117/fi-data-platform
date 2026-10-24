{{ config(materialized='table') }}
-- Dealer master: every source dealer identity -> the ONE surviving dealer (docs/V3_DESIGN.md 8.4).
-- One row per source dealer number across both admin systems (Admin Core wins a number both hold).
-- Admin systems grow duplicate identities for one dealer: an inactive copy re-keyed under a new id,
-- a prefixed-number variant ('VD1039' for D1039), and stray case / padding ('d1039 ').
-- Match rules, in order:
--   number_base   same dealer number once a letter prefix is stripped (macro dealer_number_base)
--   name_state    same normalized name and state (macro dealer_name_norm)
--   self          no other identity matches
-- Survivor of a cluster: the active identity first, then the unprefixed number, then Admin Core
-- over legacy, then the oldest dealer id / onboarding date, then the number. Every contract, fact
-- and access row resolves its dealer through this model (dim_dealer_crosswalk), so a dealer's book
-- is never split across identities.
with identities as (
    select
        'admin_core' as source_system,
        d.dealer_id as source_dealer_id,
        d.dealer_number as source_dealer_number,
        d.dealer_name,
        d.state,
        lower(d.status) = 'active' and not d.is_deleted as is_active,
        d.onboarded_date
    from {{ ref('stg_admin_core__dealer') }} as d
    where d.dealer_number is not null

    union all

    select
        'legacy_admin',
        null,
        l.dealer_code,
        l.dealer_name,
        l.state,
        coalesce(l.dealer_status, 'active') = 'active' and not l.is_deleted,
        l.onboarded_date
    from {{ ref('stg_legacy_admin__dealers') }} as l
    where l.dealer_code not in (
        select dealer_number from {{ ref('stg_admin_core__dealer') }} where dealer_number is not null
    )
),

normalized as (
    select
        *,
        {{ dealer_number_base('source_dealer_number') }} as number_base,
        {{ dealer_name_norm('dealer_name') }} as name_norm
    from identities
    -- one identity per number (a number can repeat across dealer ids in a bad extract)
    qualify row_number() over (
        partition by source_dealer_number order by is_active desc, source_dealer_id nulls last
    ) = 1
),

clustered as (
    select
        *,
        -- identities sharing a name + state join the cluster of the smallest base number among them;
        -- a prefixed variant already shares its base number
        case
            when name_norm is null or state is null then number_base
            else min(number_base) over (partition by name_norm, state)
        end as cluster_id
    from normalized
),

ranked as (
    select
        *,
        first_value(source_dealer_number) over (
            partition by cluster_id
            order by is_active desc,
                     (source_dealer_number = number_base) desc,
                     (source_system = 'admin_core') desc,
                     source_dealer_id nulls last,
                     onboarded_date nulls last,
                     source_dealer_number
        ) as surviving_dealer_number,
        count(*) over (partition by cluster_id) as identities_in_cluster
    from clustered
)

select
    source_system,
    source_dealer_id,
    source_dealer_number,
    dealer_name,
    state,
    is_active,
    number_base,
    name_norm,
    cluster_id,
    surviving_dealer_number,
    md5('dealer|' || surviving_dealer_number) as dealer_key,
    source_dealer_number = surviving_dealer_number as is_survivor,
    identities_in_cluster,
    case
        when source_dealer_number = surviving_dealer_number then 'self'
        when number_base = {{ dealer_number_base('surviving_dealer_number') }} then 'number_base'
        else 'name_state'
    end as match_rule
from ranked
