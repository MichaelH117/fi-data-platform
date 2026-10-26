-- Grain: transaction x disbursement type x account holder (the dealer-cost split).
-- reserve_amount = the CLAIMS_RESERVE bucket, profit_share_reserve = the PROFIT_SHARE_RESERVE bucket.
-- V6 edit 3: program_type (from the contract) and incentive_group route each bucket. The program fee is
-- the INSURER_FEE bucket: on a FULL_INSURED program it is insurer premium; on AGENCY and PASS_THROUGH
-- programs it is a program fee the administrator keeps, grouped as PROGRAM_FEE so incentive and
-- settlement reports never count it as premium.
select
    d.disbursement_key,
    d.txn_key,
    d.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    coalesce(md5('account_holder|' || cast(d.account_holder_id as varchar)), '-1') as account_holder_key,
    d.source_system,
    d.txn_type,
    d.disbursement_type,
    coalesce(f.program_type, 'UNKNOWN') as program_type,
    case
        when d.disbursement_type = 'INSURER_FEE' and f.program_type in ('AGENCY', 'PASS_THROUGH') then 'PROGRAM_FEE'
        when d.disbursement_type = 'INSURER_FEE' then 'INSURER_PREMIUM'
        when d.disbursement_type = 'COMMISSION' then 'PRODUCER_INCENTIVE'
        when d.disbursement_type = 'ADMIN_FEE' then 'ADMINISTRATION'
        when d.disbursement_type in ('CLAIMS_RESERVE', 'PROFIT_SHARE_RESERVE') then 'RESERVE'
        when d.disbursement_type = 'PREMIUM_TAX' then 'TAX'
        else 'OTHER'
    end as incentive_group,
    -- V7 edit 21: the line's own group (set by the program setup) when the source stores one; else
    -- the default bucket of a non-participating setup. That default is the fixed type -> group lookup
    -- the old reports used for every line, which is wrong for participating setups.
    upper(coalesce(d.source_disbursement_group,
        case d.disbursement_type
            when 'CLAIMS_RESERVE' then 'PREMIUM'
            when 'INSURER_FEE' then 'PREMIUM'
            when 'ADMIN_FEE' then 'ADMIN'
            when 'PROFIT_SHARE_RESERVE' then 'ADMIN'
            when 'PREMIUM_TAX' then 'TAX_AND_FEES'
            when 'COMMISSION' then 'PRODUCER'
            else 'OTHER'
        end)) as disbursement_group,
    d.source_disbursement_group is not null as is_group_from_line,
    d.posted_at,
    {{ date_key('d.posted_at') }} as posted_date_key,
    d.posting_period_key,
    {{ period_start_date("d.posting_period_key") }} as posting_period_start_date,
    d.amount,
    d.reserve_amount,
    d.profit_share_reserve,
    d.is_refundable,
    d.is_renewable,
    {{ audit_columns() }}
from {{ ref('int_disbursements_split') }} as d
inner join {{ ref('dim_contract') }} as k using (contract_key)
left join {{ ref('dim_contract_finance') }} as f using (contract_key)
