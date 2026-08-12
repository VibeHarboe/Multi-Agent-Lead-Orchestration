with source as (
    select * from {{ ref('customers') }}
),

renamed as (
    select
        customer_id,
        customer_name,
        country,
        segment,
        cast(signup_date as date)    as signup_date,
        plan_type,
        monthly_revenue              as mrr,
        partner_id,

        -- derived fields
        date_diff(
            'day', cast(signup_date as date), current_date
        )                            as tenure_days,

        case
            when country = 'DK' then 'Scandinavia'
            when country = 'NO' then 'Scandinavia'
            when country = 'SE' then 'Scandinavia'
            when country = 'DE' then 'DACH'
            when country = 'NL' then 'Benelux'
            when country = 'US' then 'North America'
        end                          as region

    from source
)

select * from renamed
