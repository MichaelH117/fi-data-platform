-- Section 3 recomputed straight from the state method matrix: reserve = rate x greatest(base, 0),
-- deposit = deposit rate x reserve. Returns lines that disagree by more than a cent.
select s.state, s.provider_key, s.scope, s.basis, s.calculated_reserve, s.security_deposit
from {{ ref('mart_funded_reserve_summary') }} as s
inner join {{ ref('state_reserve_method') }} as m on m.state = s.state
where abs(s.calculated_reserve - m.reserve_rate * greatest(
          case m.method_code
              when 'NET_OF_CLAIMS' then s.gross_consideration - s.claims_paid_amount
              else s.gross_consideration
          end, 0)) > 0.01
   or abs(s.security_deposit - coalesce(m.security_deposit_rate, 0) * s.calculated_reserve) > 0.01
