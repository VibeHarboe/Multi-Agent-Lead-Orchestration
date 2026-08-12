with source as (
    select * from {{ ref('upsell_events') }}
),

renamed as (
    select
        upsell_id,
        customer_id,
        cast(upsell_date as date)       as upsell_date,
        from_plan,
        to_plan,
        nullif(trim(add_on), '')        as add_on,
        add_on_revenue,
        sales_rep,
        country,

        -- type of upsell
        case
            when from_plan != to_plan       then 'plan_upgrade'
            when add_on is not null         then 'add_on_sale'
            else 'unknown'
        end                             as upsell_type,

        -- total incremental revenue from the upsell
        coalesce(add_on_revenue, 0)     as incremental_revenue

    from source
)

select * from renamed
