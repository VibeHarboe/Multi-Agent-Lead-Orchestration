with source as (
    select * from {{ ref('partner_engagement_daily') }}
),

renamed as (
    select
        partner_id,
        cast(snapshot_date as date)         as snapshot_date,
        response_latency_p50_hours,
        response_latency_p90_hours,
        accept_count,
        decline_count,
        no_response_count,
        cancellation_count,
        capacity_utilization_pct,
        partner_roi_pct_snapshot,
        net_monthly_value_snapshot,
        churn_risk_score,

        -- flags the monitor + matcher key off
        (churn_risk_score >= 65)            as is_at_risk,
        (churn_risk_score >= 80)            as is_high_risk,
        (partner_roi_pct_snapshot < 0)      as is_unprofitable,

        -- total interaction volume for the day (denominator for rates)
        (accept_count + decline_count + no_response_count)  as total_interactions

    from source
)

select * from renamed
