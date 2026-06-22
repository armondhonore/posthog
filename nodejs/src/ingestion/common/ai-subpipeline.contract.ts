import { Message } from 'node-rdkafka'

import { GroupTypeManager } from '~/common/groups/group-type-manager'
import { HogTransformer } from '~/common/hog-transformations/hog-transformer.interface'
import {
    AiEventOutput,
    AsyncOutput,
    EventOutput,
    IngestionWarningsOutput,
    PersonDistinctIdsOutput,
    PersonsOutput,
} from '~/common/outputs'
import { IngestionOutputs } from '~/common/outputs/ingestion-outputs'
import { GroupStoreForBatch } from '~/ingestion/common/groups/group-store-for-batch'
import { PersonsStoreForBatch } from '~/ingestion/common/persons/persons-store-for-batch'
import { EmitEventStepOutput } from '~/ingestion/common/steps/event-processing/emit-event-step'
import { EventPipelineRunnerOptions } from '~/ingestion/common/steps/event-processing/event-pipeline-options'
import { SplitAiEventsStepConfig } from '~/ingestion/common/steps/event-processing/split-ai-events-step'
import { PipelineBuilder, StartPipelineBuilder } from '~/ingestion/framework/builders/pipeline-builders'
import { TopHogWrapper } from '~/ingestion/framework/extensions/tophog'
import { PluginEvent } from '~/plugin-scaffold'
import { EventHeaders, Team } from '~/types'
import { TeamManager } from '~/utils/team-manager'

export interface AiEventSubpipelineInput {
    message: Message
    event: PluginEvent
    team: Team
    headers: EventHeaders
    personsStoreForBatch: PersonsStoreForBatch
    groupStoreForBatch: GroupStoreForBatch
}

// `unavailable` means the backing store errored; the verify step treats it as a
// first sighting (fail open). Defined here, on the composition contract, so the
// analytics lane can pass a store through without importing the ai lane.
export type GatewayNonceOutcome = 'first' | 'replay' | 'unavailable'

export interface GatewayNonceStore {
    markSeen(token: string, requestId: string, ttlMs: number): Promise<GatewayNonceOutcome>
}

export interface AiEventSubpipelineConfig {
    options: EventPipelineRunnerOptions
    outputs: IngestionOutputs<
        EventOutput | AiEventOutput | IngestionWarningsOutput | PersonsOutput | PersonDistinctIdsOutput
    >
    teamManager: TeamManager
    groupTypeManager: GroupTypeManager
    hogTransformer: HogTransformer
    splitAiEventsConfig: SplitAiEventsStepConfig
    topHog: TopHogWrapper
    // Optional gateway-provenance replay guard; absent disables within-window dedup.
    gatewayNonceStore?: GatewayNonceStore
}

/**
 * Abstract factory for the AI event sub-pipeline. The analytics lane composes the AI branch through
 * this contract instead of importing the `ai` lane directly; the concrete `createAiEventSubpipeline`
 * (ai lane) is injected at the composition root (servers). This keeps ai and analytics decoupled.
 */
export type AiEventSubpipelineFactory = <TInput extends AiEventSubpipelineInput, TContext>(
    builder: StartPipelineBuilder<TInput, TContext>,
    config: AiEventSubpipelineConfig
) => PipelineBuilder<TInput, EmitEventStepOutput, TContext, AsyncOutput>
