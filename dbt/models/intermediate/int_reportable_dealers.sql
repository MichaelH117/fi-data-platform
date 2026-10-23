-- Dealer-level reportability, from seeds/core/demo_exclusions.csv (dealer_flag and dealer_number
-- rules). Read by int_reportable_scope and dim_dealer; nothing else re-implements the rule.
with rules as (
    select * from {{ ref('demo_exclusions') }}
),

dealers as (
    select dealer_id, dealer_number, is_demo_dealer from {{ ref('stg_admin_core__dealer') }}
    union all
    -- legacy dealer codes that never reached Admin Core
    select null, l.dealer_code, false
    from {{ ref('stg_legacy_admin__dealers') }} as l
    where l.dealer_code not in (select dealer_number from {{ ref('stg_admin_core__dealer') }} where dealer_number is not null)
)

select
    d.dealer_id,
    d.dealer_number,
    coalesce(flag.rule_id, num.rule_id) as exclusion_rule_id,
    coalesce(flag.rule_id, num.rule_id) is null as is_reportable
from dealers as d
left join rules as flag
    on flag.rule_type = 'dealer_flag' and d.is_demo_dealer
left join rules as num
    on num.rule_type = 'dealer_number' and num.dealer_number = d.dealer_number
qualify row_number() over (partition by d.dealer_number order by d.dealer_id nulls last) = 1
