-- State funded-reserve / provider renewal filing summary
-- (grain: state x provider x scope (ITD | PERIOD) x basis (COVERAGE_COST | RETAIL)).
-- Only states with a requirement in seeds/regulatory/state_reserve_method appear; the rest are skipped.
-- Section 2 (business activity) does not depend on the basis; Section 3 (computation) does.
-- Claims paid has two populations on purpose (seed notes say which): Section 2 count and amount cover
-- all in-scope contracts (claims_paid_amount_all, the like-for-like dollars for the count); Section 3
-- claims_paid_amount covers the in-force block only and is a subset of claims_paid_amount_all.
--   net_consideration  = gross_consideration - claims_paid_amount
--   calculated_reserve = reserve_rate x greatest(method base, 0); base = net (NET_OF_CLAIMS) or gross
--   security_deposit   = security_deposit_rate x calculated_reserve
--   current_reserve    = remaining balance: reserve funded at sale on the in-force block minus claims
--                        paid on it up to the period end (line 3.6; see the seed note)
--   reserve_shortfall  = greatest(calculated_reserve - current_reserve, 0)
-- notes: the methodology column (basis, preliminary vs final, provider basis, genuine zeros).
with v as (
    select * from {{ ref('mart_regulatory_line_values') }}
    where report_id = 'FUNDED_RESERVE'
),

bases as (
    select unnest(['COVERAGE_COST', 'RETAIL']) as basis
),

keys as (
    select distinct state, provider_key, provider_name, scope, window_label, window_start, window_end, is_final
    from v
),

pivoted as (
    select
        k.*,
        b.basis,
        sum(v.value) filter (where v.line_item = 'IN_FORCE') as contracts_in_force,
        sum(v.value) filter (where v.line_item = 'NEW_WRITTEN') as new_contracts_written,
        sum(v.value) filter (where v.line_item = 'COMPLAINTS') as complaints_received,
        sum(v.value) filter (where v.line_item = 'CLAIMS_PAID_COUNT') as claims_paid_count,
        sum(v.value) filter (where v.line_item = 'CLAIMS_DENIED') as claims_denied_count,
        sum(v.value) filter (where v.line_item = 'CLAIMS_PAID_AMOUNT_ALL') as claims_paid_amount_all,
        sum(v.value) filter (where v.line_item = 'GROSS_CONSIDERATION' and v.basis = b.basis) as gross_consideration,
        sum(v.value) filter (where v.line_item = 'CLAIMS_PAID_AMOUNT') as claims_paid_amount,
        sum(v.value) filter (where v.line_item = 'CURRENT_RESERVE') as current_reserve
    from keys as k
    cross join bases as b
    inner join v
        on v.state = k.state and v.provider_key = k.provider_key and v.scope = k.scope
    group by all
),

mapped as (
    -- contracts assigned to the provider by the state provider map (legacy book, no obligor)
    select state, provider_key, count(*) as mapped_contracts
    from {{ ref('mart_funded_reserve_contracts') }}
    where provider_basis = 'STATE_PROVIDER_MAP' and is_filing_product_scope and is_written_itd
    group by all
),

calc as (
    select
        p.*,
        m.method_code,
        m.reserve_rate,
        coalesce(m.security_deposit_rate, 0) as security_deposit_rate,
        p.basis = m.default_basis as is_default_basis,
        p.gross_consideration - p.claims_paid_amount as net_consideration,
        round(m.reserve_rate * greatest(
            case m.method_code
                when 'NET_OF_CLAIMS' then p.gross_consideration - p.claims_paid_amount
                when 'GROSS_CONSIDERATION' then p.gross_consideration
            end, 0), 2) as calculated_reserve,
        coalesce(x.mapped_contracts, 0) as mapped_contracts,
        m.notes as method_notes
    from pivoted as p
    inner join {{ ref('state_reserve_method') }} as m on m.state = p.state and m.has_requirement
    left join mapped as x on x.state = p.state and x.provider_key = p.provider_key
)

select
    {{ surrogate_key(['state', 'provider_key', 'scope', 'basis']) }} as filing_line_key,
    state,
    provider_key,
    provider_name,
    scope,
    basis,
    is_default_basis,
    window_label as period_label,
    window_start as period_start,
    window_end as period_end,
    is_final,
    method_code,
    reserve_rate,
    security_deposit_rate,
    -- Section 2: business activity
    contracts_in_force,
    new_contracts_written,
    complaints_received,
    claims_paid_count,
    claims_denied_count,
    claims_paid_amount_all,
    -- Section 3: computation
    gross_consideration,
    claims_paid_amount,
    net_consideration,
    calculated_reserve,
    round(security_deposit_rate * calculated_reserve, 2) as security_deposit,
    current_reserve,
    round(greatest(calculated_reserve - current_reserve, 0), 2) as reserve_shortfall,
    mapped_contracts,
    concat_ws('; ',
        case when basis = 'COVERAGE_COST' then 'Basis: coverage cost' else 'Basis: retail price' end
            || case when is_default_basis then ' (state default)' else ' (supplemental)' end,
        case when is_final then 'FINAL: period end is in a closed posting period'
             else 'PRELIMINARY: period end is in an open posting period' end,
        case when mapped_contracts > 0 then
            cast(mapped_contracts as varchar) || ' legacy contracts assigned by the state provider map '
            || '(no obligor on the legacy record)' end,
        case when provider_key = '-1' then 'Provider unresolved: obligor missing and no state provider map' end,
        case when contracts_in_force = 0 and new_contracts_written = 0 then 'Genuine zero: no activity in the window'
        end,
        case when claims_paid_count = 0 and contracts_in_force > 0 then
            'Genuine zero: no claims closed with payment in the window' end,
        case when complaints_received = 0 then 'Genuine zero: no complaints received in the window' end,
        case when net_consideration < 0 then 'Claims exceed consideration; reserve floored at 0' end,
        method_notes
    ) as notes,
    {{ audit_columns() }}
from calc
