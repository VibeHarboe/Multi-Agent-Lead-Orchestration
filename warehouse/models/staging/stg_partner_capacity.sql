with source as (
    select * from {{ ref('partner_capacity') }}
),

renamed as (
    select
        partner_id,
        cast(snapshot_date as date)     as snapshot_date,
        active_deals_count,
        soft_cap,
        hard_cap,

        -- utilisation as pct of soft cap
        round(
            active_deals_count / nullif(soft_cap, 0) * 100.0,
            2
        )                               as utilization_pct,

        -- flags for the matching agent
        (active_deals_count < soft_cap) as is_under_soft_cap,
        (active_deals_count < hard_cap) as is_under_hard_cap

    from source
)

select * from renamed
