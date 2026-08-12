/*
  mart_referring_partner_roi.sql  (Q20)
  -------------------------------------
  The ambassador-side ROI — symmetrical to mart_roi_partnerships on the
  fulfillment side.

  referring_partner_net_value = LTV of converted leads − fees paid

  v1 heuristics (documented, deliberately simple):
  - LTV proxy: 12 months of the converted customer's MRR. A converted lead is
    a referral with a customer_id (the SMB became a NordLedger customer).
  - Fees paid: monthly_fee_paid × 24 (the simulation's window). Pay-per-lead
    referrers (fee 0) therefore always have non-negative net value.
*/

with referring as (
    select * from {{ ref('stg_referring_partners') }}
),

converted as (
    select
        rl.referring_partner_id,
        count(*)                                       as leads_referred,
        count(*) filter (where rl.is_booked)           as leads_booked,
        count(*) filter (where rl.is_converted)        as leads_converted,
        round(
            count(*) filter (where rl.is_converted)
            / nullif(count(*), 0) * 100.0, 2
        )                                              as conversion_rate_pct,
        coalesce(sum(c.mrr) filter (where rl.is_converted), 0)
                                                       as converted_mrr
    from {{ ref('stg_referral_leads') }} rl
    left join {{ ref('stg_customers') }} c
        on rl.customer_id = c.customer_id
    group by 1
),

rollup as (
    select
        r.referring_partner_id,
        r.referring_partner_name,
        r.country,
        r.referring_partner_type,
        r.status,
        r.monthly_fee_paid,
        coalesce(c.leads_referred, 0)      as leads_referred,
        coalesce(c.leads_booked, 0)        as leads_booked,
        coalesce(c.leads_converted, 0)     as leads_converted,
        coalesce(c.conversion_rate_pct, 0) as conversion_rate_pct,
        coalesce(c.converted_mrr, 0)       as converted_mrr,

        -- LTV proxy: 12 months of converted MRR
        coalesce(c.converted_mrr, 0) * 12  as ltv_estimate,

        -- fees over the simulation window (24 months)
        r.monthly_fee_paid * 24            as fees_paid_estimate,

        -- Q20: the net value
        coalesce(c.converted_mrr, 0) * 12
        - r.monthly_fee_paid * 24          as referring_partner_net_value

    from referring r
    left join converted c using (referring_partner_id)
)

select * from rollup
order by referring_partner_net_value desc
