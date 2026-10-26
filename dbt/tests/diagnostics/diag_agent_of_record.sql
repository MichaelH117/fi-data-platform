-- Diagnostic (V7 edit 24): agent of record. The measured value (pipeline diagnostics) is how many
-- contracts name a different agent than their dealer's current agent (a rep reassignment, or an
-- error); that count never fails, it feeds the agency roll-up page. The check fails when a
-- contract's source agent id resolves to no agent at all (the roll-up would lose the contract).
select c.contract_key, c.source_system
from {{ ref('dim_contract') }} as c
inner join {{ ref('int_contracts_conformed') }} as k using (contract_key)
where c.agent_key = '-1' and k.agent_id is not null
