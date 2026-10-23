-- Every effectively cancelled contract is closed out, bucket by bucket: what was written is either
-- earned (monthly earning to the cancel date plus the cancel true-up) or refunded, to the cent.
-- Nothing stays unearned forever.
with written as (
    select contract_key, disbursement_type,
           sum(amount) filter (where txn_type in ('SALE', 'AMEND')) as written,
           -coalesce(sum(amount) filter (where txn_type in ('CANCEL', 'REINSTATE')), 0) as refunded
    from {{ ref('fct_disbursement') }}
    group by 1, 2
),

earned as (
    select contract_key, disbursement_type, sum(earned_amount) as earned
    from {{ ref('fct_earned_disbursement') }}
    group by 1, 2
),

cancelled as (
    select distinct contract_key
    from {{ ref('int_cancellations_conformed') }}
    where is_effective_cancel
)

select w.contract_key, w.disbursement_type, w.written, coalesce(e.earned, 0) as earned, w.refunded
from written as w
inner join cancelled as c using (contract_key)
inner join {{ ref('dim_contract') }} as k using (contract_key)
left join earned as e using (contract_key, disbursement_type)
where w.written is not null
  and abs(w.written - coalesce(e.earned, 0) - w.refunded) > 0.01
