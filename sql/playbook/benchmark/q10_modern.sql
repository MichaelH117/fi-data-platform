-- database: modern
-- timeout: 60s
-- question: Share of contracts whose vehicle attributes are found, joined on the surrogate key.
-- fix:      vehicles join on contract_key (an enforced foreign key); the VIN is normalized and hashed once in
--           staging, so no lookup ever compares hand-typed text.
set statement_timeout = '60s';

select round(100.0 * count(v.contract_key) / count(*), 2) as match_rate_pct, count(*) as contracts
from core.dim_contract c
left join core.dim_contract_vehicle v on v.contract_key = c.contract_key;
