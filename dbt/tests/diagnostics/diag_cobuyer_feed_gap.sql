-- Diagnostic (V6 edit 9): co-buyer in the e-contracting feed, missing in the admin system. The admin
-- contract row has no co-buyer field, so the feed is the only source; the measured value (pipeline
-- diagnostics) is how many conformed contracts carry a feed co-buyer. The check fails when the
-- join breaks: a feed deal with a co-buyer matches an Admin Core contract that does not carry the flag.
select c.contract_key, c.deal_id
from {{ ref('int_contracts_conformed') }} as c
inner join {{ ref('stg_network_feed__deals') }} as d on d.deal_id = c.deal_id
where c.source_system = 'admin_core'
  and d.cobuyer_name is not null
  and not c.has_feed_cobuyer
