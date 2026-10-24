-- database: modern
-- timeout: 60s
-- question: The reportable in-force population for a compliance filing, with each contract's agent (rep).
-- fix:      test, API and demo contracts are excluded once in int_reportable_scope, and the agent is a key on
--           the contract (agent_key), not a join on a shared dealer number, so nothing fans out.
set statement_timeout = '60s';

select count(*) as filing_rows, count(distinct c.contract_key) as distinct_contracts
from core.dim_contract c
left join core.dim_agent a on a.agent_key = c.agent_key
where c.status = 'INFORCE';
