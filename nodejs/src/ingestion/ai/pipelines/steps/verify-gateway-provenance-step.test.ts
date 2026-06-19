import { createHmac } from 'node:crypto'

import { PluginEvent } from '~/plugin-scaffold'

import { EventHeaders } from '../../../../types'
import { UUIDT } from '../../../../utils/utils'
import { PipelineResultType } from '../../../pipelines/results'
import { createVerifyGatewayProvenanceStep } from './verify-gateway-provenance-step'

const SECRET = 'test-signing-secret'
const TOKEN = 'phc_test'
const DISTINCT_ID = 'user-7'
const MAX_AGE_MS = 5 * 60 * 1000

// Mirror of ai-gateway internal/emitter/signer.go: HMAC-SHA256 over
// `token \n distinct_id \n request_id \n signed_at`, lowercase hex.
function sign(token: string, distinctId: string, requestId: string, signedAt: string, secret = SECRET): string {
    return createHmac('sha256', secret).update(`${token}\n${distinctId}\n${requestId}\n${signedAt}`).digest('hex')
}

function createEvent(properties: Record<string, any>): PluginEvent {
    return {
        distinct_id: DISTINCT_ID,
        ip: null,
        site_url: 'http://localhost',
        team_id: 1,
        now: '2026-05-28T10:00:00Z',
        timestamp: '2026-05-28T10:00:00Z',
        event: '$ai_generation',
        uuid: new UUIDT().toString(),
        properties,
    }
}

function headers(token: string | undefined = TOKEN): EventHeaders {
    return {
        token,
        force_disable_person_processing: false,
        historical_migration: false,
        skip_heatmap_processing: false,
    }
}

async function run(
    properties: Record<string, any>,
    opts: { secret?: string; token?: string } = {}
): Promise<Record<string, any>> {
    const step = createVerifyGatewayProvenanceStep(opts.secret ?? SECRET, MAX_AGE_MS)
    const normalizedEvent = createEvent(properties)
    const result = await step({ normalizedEvent, headers: headers(opts.token) })
    if (result.type !== PipelineResultType.OK) {
        throw new Error(`expected OK, got ${result.type}`)
    }
    return result.value.normalizedEvent.properties!
}

describe('verifyGatewayProvenanceStep', () => {
    it('stamps $ai_gateway_verified and drops artifacts for a valid, fresh signature', async () => {
        const signedAt = new Date().toISOString()
        const props = await run({
            $ai_gateway: true,
            $ai_gateway_request_id: 'req-123',
            $ai_gateway_signed_at: signedAt,
            $ai_gateway_signature: sign(TOKEN, DISTINCT_ID, 'req-123', signedAt),
            $ai_model: 'claude',
        })

        expect(props.$ai_gateway_verified).toBe(true)
        expect(props.$ai_gateway_signature).toBeUndefined()
        expect(props.$ai_gateway_signed_at).toBeUndefined()
        expect(props.$ai_model).toBe('claude')
    })

    it('verifies when request_id is absent (signed over empty string)', async () => {
        const signedAt = new Date().toISOString()
        const props = await run({
            $ai_gateway: true,
            $ai_gateway_signed_at: signedAt,
            $ai_gateway_signature: sign(TOKEN, DISTINCT_ID, '', signedAt),
        })
        expect(props.$ai_gateway_verified).toBe(true)
    })

    it.each([
        [
            'an invalid signature',
            (signedAt: string) => ({
                $ai_gateway: true,
                $ai_gateway_signed_at: signedAt,
                $ai_gateway_signature: 'deadbeef',
            }),
        ],
        ['a missing signature', (signedAt: string) => ({ $ai_gateway: true, $ai_gateway_signed_at: signedAt })],
        [
            'a stale signed_at',
            () => {
                const old = new Date(Date.now() - MAX_AGE_MS - 1000).toISOString()
                return {
                    $ai_gateway: true,
                    $ai_gateway_signed_at: old,
                    $ai_gateway_signature: sign(TOKEN, DISTINCT_ID, '', old),
                }
            },
        ],
    ])('strips the whole $ai_gateway* namespace for %s', async (_name, build) => {
        const signedAt = new Date().toISOString()
        const props = await run({ ...build(signedAt), $ai_keep: 'yes' })

        expect(Object.keys(props).some((k) => k.startsWith('$ai_gateway'))).toBe(false)
        expect(props.$ai_gateway_verified).toBeUndefined()
        expect(props.$ai_keep).toBe('yes')
    })

    it('strips a client-forged $ai_gateway_verified that has no valid signature', async () => {
        const props = await run({ $ai_gateway: true, $ai_gateway_verified: true })
        expect(props.$ai_gateway_verified).toBeUndefined()
    })

    it('strips $ai_gateway* when no secret is configured, even with an otherwise-valid signature', async () => {
        const signedAt = new Date().toISOString()
        const props = await run(
            {
                $ai_gateway: true,
                $ai_gateway_signed_at: signedAt,
                $ai_gateway_signature: sign(TOKEN, DISTINCT_ID, '', signedAt),
            },
            { secret: '' }
        )
        expect(Object.keys(props).some((k) => k.startsWith('$ai_gateway'))).toBe(false)
    })

    it('leaves events without any $ai_gateway* properties untouched', async () => {
        const props = await run({ $ai_model: 'gpt-4', $ai_input_tokens: 10 })
        expect(props).toEqual({ $ai_model: 'gpt-4', $ai_input_tokens: 10 })
    })
})
