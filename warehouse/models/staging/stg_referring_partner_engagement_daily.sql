with source as (
    select * from {{ ref('referring_partner_engagement_daily') }}
),

renamed as (
    select
        referring_partner_id,
        cast(snapshot_date as date)             as snapshot_date,
        leads_sent_count,
        conversion_rate_pct,
        avg_deal_size,
        cancellation_rate_pct,
        inquiry_response_latency_hours,
        churn_risk_score,

        -- flags symmetrical to fulfillment side
        (churn_risk_score >= 65)                as is_at_risk,
        (churn_risk_score >= 80)                as is_high_risk,

        -- lead-quality quick tier for the weekly report
        case
            when conversion_rate_pct >= 60 then 'high_quality'
            when conversion_rate_pct >= 40 then 'average'
            else 'low_quality'
        end                                     as lead_quality_tier

    from source
)

select * from renamed
