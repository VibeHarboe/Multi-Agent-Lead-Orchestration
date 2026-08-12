with source as (
    select * from {{ ref('referring_partners') }}
),

renamed as (
    select
        referring_partner_id,
        referring_partner_name,
        country,
        referring_partner_type,
        cast(contract_start_date as date)   as contract_start_date,
        monthly_fee_paid,
        status,

        -- tenure in months
        date_diff('month',
            cast(contract_start_date as date),
            current_date
        )                                   as referrer_tenure_months

    from source
)

select * from renamed
