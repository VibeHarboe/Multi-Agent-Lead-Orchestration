/*
  mart_roi_partnerships.sql
  -------------------------
  KPI dashboard: ROI on Partnerships

  Key metrics:
  - Revenue generated via each partner (MRR from referred customers)
  - Partner cost (monthly fee + commission paid)
  - Net ROI per partner
  - Customer lifetime value by acquisition channel (partner vs organic)
  - Partner conversion rate
  - Payback period per partner (months)

  Ported from the original Snowflake SQL to DuckDB for the local simulation:
  countif() -> count(*) filter (where ...), iff() -> case when,
  datediff() -> date_diff(), current_date() -> current_date.
*/

with partners as (
    select * from {{ ref('stg_partners') }}
),

customers as (
    select * from {{ ref('stg_customers') }}
),

subscriptions as (
    select * from {{ ref('stg_subscriptions') }}
),

leads as (
    select * from {{ ref('stg_leads') }}
),

-- revenue attributed to each partner via referred customers
partner_revenue as (
    select
        c.partner_id,
        count(distinct c.customer_id)                       as total_customers,
        count(*) filter (where s.is_active)                 as active_customers,
        count(*) filter (where s.is_churned)                as churned_customers,

        sum(case when s.is_active then s.mrr else 0 end)    as active_mrr,
        sum(s.mrr)                                          as total_mrr_ever,

        -- estimated LTV: avg active MRR * avg tenure months
        round(
            avg(case when s.is_active then s.mrr else 0 end)
            * avg(date_diff('month', s.start_date,
                            coalesce(s.end_date, current_date))),
            2
        )                                                   as estimated_avg_ltv,

        -- commission owed: active MRR * commission rate
        round(
            sum(case when s.is_active then s.mrr else 0 end)
            * max(p.commission_rate), 2
        )                                                   as monthly_commission_owed

    from customers c
    inner join subscriptions s
        on c.customer_id = s.customer_id
    inner join partners p
        on c.partner_id = p.partner_id
    group by 1
),

-- leads from partner channel
partner_leads as (
    select
        c.partner_id,
        count(*)                                    as partner_leads,
        count(*) filter (where l.is_converted)      as partner_lead_conversions,
        round(
            count(*) filter (where l.is_converted)
            / nullif(count(*), 0) * 100, 2
        )                                           as partner_conversion_rate_pct
    from leads l
    inner join customers c
        on l.customer_id = c.customer_id
    where l.lead_source = 'partner'
    group by 1
),

-- full ROI calculation
partner_roi as (
    select
        p.partner_id,
        p.partner_name,
        p.country,
        p.partner_type,
        p.contract_start_date,
        p.monthly_fee,
        p.commission_rate,
        p.partner_tenure_months,

        coalesce(pr.total_customers, 0)             as total_customers,
        coalesce(pr.active_customers, 0)            as active_customers,
        coalesce(pr.churned_customers, 0)           as churned_customers,
        coalesce(pr.active_mrr, 0)                  as active_mrr,
        coalesce(pr.total_mrr_ever, 0)              as total_mrr_ever,
        coalesce(pr.estimated_avg_ltv, 0)           as estimated_avg_ltv,
        coalesce(pr.monthly_commission_owed, 0)     as monthly_commission_owed,

        coalesce(pl.partner_leads, 0)               as partner_leads,
        coalesce(pl.partner_lead_conversions, 0)    as partner_lead_conversions,
        coalesce(pl.partner_conversion_rate_pct, 0) as partner_conversion_rate_pct,

        -- total monthly cost = fixed fee + commission
        p.monthly_fee
        + coalesce(pr.monthly_commission_owed, 0)   as total_monthly_cost,

        -- net monthly value = revenue - cost
        coalesce(pr.active_mrr, 0)
        - (p.monthly_fee + coalesce(pr.monthly_commission_owed, 0))
                                                    as net_monthly_value,

        -- ROI % = (revenue - cost) / cost * 100
        round(
            (
                coalesce(pr.active_mrr, 0)
                - (p.monthly_fee + coalesce(pr.monthly_commission_owed, 0))
            )
            / nullif(
                p.monthly_fee + coalesce(pr.monthly_commission_owed, 0),
                0
            ) * 100,
            2
        )                                           as roi_pct,

        -- payback period in months = cumulative cost / monthly net value
        round(
            (p.monthly_fee * p.partner_tenure_months)
            / nullif(coalesce(pr.active_mrr, 0), 0),
            1
        )                                           as payback_period_months,

        -- cost per acquired customer
        round(
            (p.monthly_fee * p.partner_tenure_months)
            / nullif(coalesce(pr.total_customers, 0), 0),
            2
        )                                           as cost_per_acquired_customer

    from partners p
    left join partner_revenue pr on p.partner_id = pr.partner_id
    left join partner_leads pl   on p.partner_id = pl.partner_id
)

select * from partner_roi
order by roi_pct desc
