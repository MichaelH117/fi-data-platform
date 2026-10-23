-- Earned never exceeds written, and a contract that expired uncancelled before the as-of date is
-- fully earned (every disbursement type, every contract).
with earned as (
    select contract_key, disbursement_type, max(written_amount) as written, sum(earned_amount) as earned
    from {{ ref('fct_earned_disbursement') }}
    group by 1, 2
)

select e.contract_key, e.disbursement_type, e.written, e.earned
from earned as e
inner join {{ ref('dim_contract') }} as k using (contract_key)
cross join {{ ref('int_as_of_date') }} as a
where abs(e.earned) > abs(e.written) + 0.01
   or (not k.is_cancelled and k.expiration_date <= a.as_of_date and abs(e.earned - e.written) > 0.01)
