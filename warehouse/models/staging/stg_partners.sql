with source as (
    select * from {{ ref('partners') }}
),

renamed as (
    select
        partner_id,
        partner_name,
        country,
        partner_type,
        cast(contract_start_date as date)   as contract_start_date,
        monthly_fee,
        commission_rate,
        status,

        -- partner tenure in months
        date_diff('month',
            cast(contract_start_date as date),
            current_date
        )                                   as partner_tenure_months

    from source
)

select * from renamed
