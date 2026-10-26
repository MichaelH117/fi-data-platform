-- New Reserves (V6 edit 11): new reserves by product and term length. Grain: tenant x entered month x
-- product category x term-year bucket (seed term_year_buckets). new_reserve = the GROSS claims reserve
-- booked at sale (CLAIMS_RESERVE, txn SALE) on contracts entered in the month, before any cancel: the
-- basis a reinsurer's new-business report uses. mart_reserve_bridge walks it to the Experience page's
-- net written. Sales posted after the latest closed period are left out, as on every statement.
-- row_pct = this bucket's share of the category's new reserve in the month (the "% by term year" matrix);
-- term_months_total lets a page recompute the average term over any months it sums.
with as_of as (
    select max(posting_period_key) as as_of_period_key
    from {{ ref('dim_posting_period') }}
    where is_closed
),

buckets as (
    select * from {{ ref('term_year_buckets') }}
),

sales as (
    select d.contract_key, sum(d.amount) as new_reserve
    from {{ ref('fct_disbursement') }} as d
    cross join as_of as a
    where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'SALE'
        and d.posting_period_key <= a.as_of_period_key and d.contract_key::varchar <> '-1'
    group by 1
),

cells as (
    select
        k.tenant_key,
        cast(date_trunc('month', k.entered_date) as date) as entered_month,
        p.category_code,
        p.category,
        coalesce(b.term_bucket, 'UNKNOWN') as term_bucket,
        coalesce(b.bucket_label, 'Unknown term') as bucket_label,
        coalesce(b.sort_order, 99) as bucket_sort,
        count(*) as contracts,
        sum(s.new_reserve) as new_reserve,
        sum(t.term_months) as term_months_total
    from sales as s
    inner join {{ ref('dim_contract') }} as k using (contract_key)
    left join {{ ref('dim_contract_terms') }} as t using (contract_key)
    left join {{ ref('dim_product') }} as p on p.product_key = k.product_key
    left join buckets as b on t.term_months between b.min_months and b.max_months
    group by all
)

select
    a.as_of_period_key,
    c.*,
    {{ safe_divide('c.new_reserve', 'c.contracts') }} as avg_reserve,
    {{ safe_divide('c.term_months_total', 'c.contracts') }} as avg_term_months,
    {{ safe_divide('c.new_reserve', 'sum(c.new_reserve) over (partition by c.tenant_key, c.entered_month, c.category_code)') }}
        as row_pct,
    {{ audit_columns() }}
from cells as c
cross join as_of as a
