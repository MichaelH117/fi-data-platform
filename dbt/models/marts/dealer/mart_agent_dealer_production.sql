-- V7 edit 24: agency roll-up and rep reassignment. "Every store an agent brought in, since a date you
-- choose." Grain: agent x dealer x entered month (the page sums the months on or after the chosen
-- start date; no ITD / LTM / YTD default). Written business uses the DEALER_PRODUCTION status set.
-- Agency columns come from dim_agent (DE Manager): agency_name, principal_name, is_house_agent.
--   first_entered_date / last_entered_date  first and last sale by this agent at this dealer, all time
--   is_agent_of_record  the dealer's current agent (dim_dealer.agent_key) is this agent; a FALSE row is
--                       business an agent wrote at a store that now belongs to another rep
with written as (
    select c.contract_key, c.tenant_key, c.agent_key, c.dealer_key, c.entered_date
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'DEALER_PRODUCTION' and s.status = c.status
    where c.contract_key <> '-1'
),

span as (
    select agent_key, dealer_key, min(entered_date) as first_entered_date, max(entered_date) as last_entered_date
    from written
    group by 1, 2
)

select
    w.tenant_key,
    w.agent_key,
    coalesce(a.agent_code, 'UNASSIGNED') as agent_code,
    coalesce(a.agent_name, 'Unassigned') as agent_name,
    coalesce(nullif(trim(a.agency_name), ''), 'No agency') as agency_name,
    a.principal_name,
    coalesce(a.is_house_agent, false) as is_house_agent,
    w.dealer_key,
    d.dealer_number,
    d.dealer_name,
    d.state as dealer_state,
    coalesce(d.agent_key = w.agent_key, false) as is_agent_of_record,
    cast(date_trunc('month', w.entered_date) as date) as month_start,
    {{ is_in_progress_month('w.entered_date', "(select as_of_date from " ~ ref('dim_reporting_date') ~ ")") }} as is_in_progress_month,
    sp.first_entered_date,
    sp.last_entered_date,
    count(*) as contracts_written,
    {{ audit_columns() }}
from written as w
left join {{ ref('dim_agent') }} as a on a.agent_key = w.agent_key
left join {{ ref('dim_dealer') }} as d on d.dealer_key = w.dealer_key
left join span as sp on sp.agent_key = w.agent_key and sp.dealer_key = w.dealer_key
group by all
