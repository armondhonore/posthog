export interface EventPipelineRunnerOptions {
    SKIP_UPDATE_EVENT_AND_PROPERTIES_STEP: boolean
    PERSON_MERGE_MOVE_DISTINCT_ID_LIMIT: number
    PERSON_MERGE_ASYNC_ENABLED: boolean
    PERSON_MERGE_SYNC_BATCH_SIZE: number
    PERSON_JSONB_SIZE_ESTIMATE_ENABLE: number
    PERSON_PROPERTIES_UPDATE_ALL: boolean
    /** Teams whose $feature_flag_called events default to personless: '*' for all, '' to disable, or comma-separated team IDs */
    FLAG_CALLED_PERSONLESS_DEFAULT_TEAMS: string
    /** Shared HMAC key verifying AI-gateway provenance signatures; '' disables verification (all $ai_gateway* stripped). */
    AI_GATEWAY_SIGNING_SECRET: string
    /** Max age of a gateway signed_at timestamp before its signature is rejected, in ms. */
    AI_GATEWAY_SIGNATURE_MAX_AGE_MS: number
}
