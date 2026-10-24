-- Dealer crosswalk: every source dealer identity (both admin systems) -> the surviving dealer_key in
-- dim_dealer, with the rule that matched it (int_dealer_survivorship). Look a dealer up HERE, by any
-- of its numbers, instead of filtering one id and missing its variants. '-1' maps the unknown dealer.
select
    md5('dealer_xwalk|' || s.source_system || '|' || s.source_dealer_number) as dealer_crosswalk_key,
    s.source_system,
    s.source_dealer_id,
    s.source_dealer_number,
    s.dealer_key,
    s.surviving_dealer_number,
    s.match_rule,
    s.is_survivor,
    s.is_active as is_source_active,
    s.identities_in_cluster,
    {{ audit_columns() }}
from {{ ref('int_dealer_survivorship') }} as s
inner join {{ ref('dim_dealer') }} as d using (dealer_key)

union all

select
    '-1', null, null, 'UNKNOWN', '-1', 'UNKNOWN', 'unknown', true, false, 1, current_timestamp
