with source as (
    select * from {{ ref('nps_surveys') }}
),

renamed as (
    select
        survey_id,
        customer_id,
        cast(survey_date as date)   as survey_date,
        nps_score,
        csat_score,
        country,
        comment,

        -- NPS segment classification
        case
            when nps_score >= {{ var('nps_high_score') }}  then 'promoter'
            when nps_score >= 7                            then 'passive'
            when nps_score <= {{ var('nps_low_score') }}   then 'detractor'
        end                         as nps_category,

        -- CSAT binary: satisfied = 4 or 5
        (csat_score >= 4)           as is_satisfied

    from source
)

select * from renamed
