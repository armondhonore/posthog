import { createHmac, timingSafeEqual } from 'node:crypto'
import { Counter } from 'prom-client'

import { GatewayNonceStore } from '~/ingestion/common/ai-subpipeline.contract'
import { ok } from '~/ingestion/framework/results'
import { ProcessingStep } from '~/ingestion/framework/steps'
import { PluginEvent } from '~/plugin-scaffold'
import { EventHeaders } from '~/types'

// $ai_gateway* properties are client-settable on the public capture path,
// so nothing downstream may trust them until this step has verified them.
const GATEWAY_PREFIX = '$ai_gateway'
const SIGNATURE_PROPERTY = '$ai_gateway_signature'
const SIGNED_AT_PROPERTY = '$ai_gateway_signed_at'
const REQUEST_ID_PROPERTY = '$ai_gateway_request_id'
// VERIFIED_PROPERTY is the trusted marker billing reads. It is only ever
// set by this step; a client-supplied value is stripped (it shares the
// $ai_gateway* prefix), so it cannot be forged.
const VERIFIED_PROPERTY = '$ai_gateway_verified'

const gatewayDedupTotal = new Counter({
    name: 'gateway_provenance_dedup_total',
    help: 'Gateway provenance nonce dedup outcomes (first sighting, replay, store unavailable, missing nonce).',
    labelNames: ['outcome'],
})

type VerifyGatewayProvenanceInput = {
    normalizedEvent: PluginEvent
    headers: EventHeaders
}

// createVerifyGatewayProvenanceStep verifies the gateway's HMAC signature
// and replaces the forgeable $ai_gateway* markers with a single trusted
// $ai_gateway_verified. On any failure it strips the whole $ai_gateway*
// namespace, so a forged marker never reaches storage or billing.
//
// secret is the shared HMAC key (empty disables verification — everything
// is treated as unverified and stripped). maxAgeMs bounds how far signed_at
// may be from the event's capture time (the server-stamped `now` header, so
// ingestion lag is irrelevant), defending against replaying a captured
// signature outside that window. dedupStore, when supplied, closes the
// within-window gap: a signed request_id is single-use, so a second event
// reusing the tuple is stripped. Without a store, freshness is the only bound.
export function createVerifyGatewayProvenanceStep<TInput extends VerifyGatewayProvenanceInput>(
    secret: string,
    maxAgeMs: number,
    dedupStore?: GatewayNonceStore
): ProcessingStep<TInput, TInput> {
    const key = Buffer.from(secret)
    // A signature stays fresh for signedAt ± maxAgeMs, so a nonce first seen at
    // the window's open edge must be remembered until its close edge: 2×maxAgeMs.
    const dedupTtlMs = maxAgeMs * 2

    return async function verifyGatewayProvenanceStep(input) {
        const properties = input.normalizedEvent.properties ?? {}
        const gatewayKeys = Object.keys(properties).filter((k) => k.startsWith(GATEWAY_PREFIX))
        if (gatewayKeys.length === 0) {
            return ok(input)
        }

        const token = input.headers.token
        const trusted =
            secret !== '' &&
            isTrusted(key, token, input.normalizedEvent.distinct_id, input.headers.now, properties, maxAgeMs)

        if (trusted && !(await isReplay(dedupStore, token, properties, dedupTtlMs))) {
            // Drop the verification artifacts and stamp the trusted marker
            // ourselves, overwriting any client-supplied value.
            delete properties[SIGNATURE_PROPERTY]
            delete properties[SIGNED_AT_PROPERTY]
            properties[VERIFIED_PROPERTY] = true
        } else {
            for (const k of gatewayKeys) {
                delete properties[k]
            }
        }
        return ok(input)
    }
}

// Records the request_id nonce and reports a reuse; a store outage returns
// false (fail open). token scopes the nonce across projects.
async function isReplay(
    dedupStore: GatewayNonceStore | undefined,
    token: string | undefined,
    properties: Record<string, any>,
    ttlMs: number
): Promise<boolean> {
    if (!dedupStore || typeof token !== 'string') {
        return false
    }
    const requestId = typeof properties[REQUEST_ID_PROPERTY] === 'string' ? properties[REQUEST_ID_PROPERTY] : ''
    if (requestId === '') {
        // The gateway always emits a unique request_id, so a valid signature
        // over an empty one shouldn't occur; nothing to dedup on, so allow it.
        gatewayDedupTotal.inc({ outcome: 'no_request_id' })
        return false
    }
    const outcome = await dedupStore.markSeen(token, requestId, ttlMs)
    gatewayDedupTotal.inc({ outcome })
    return outcome === 'replay'
}

function isTrusted(
    key: Buffer,
    token: string | undefined,
    distinctId: string,
    capturedAt: Date | undefined,
    properties: Record<string, any>,
    maxAgeMs: number
): boolean {
    const signature = properties[SIGNATURE_PROPERTY]
    const signedAt = properties[SIGNED_AT_PROPERTY]
    if (typeof token !== 'string' || typeof signature !== 'string' || typeof signedAt !== 'string') {
        return false
    }
    const requestId = typeof properties[REQUEST_ID_PROPERTY] === 'string' ? properties[REQUEST_ID_PROPERTY] : ''
    return (
        verifySignature(key, token, distinctId, requestId, signedAt, signature) &&
        isFresh(signedAt, capturedAt, maxAgeMs)
    )
}

// verifySignature mirrors the gateway's canonical form byte-for-byte:
// HMAC-SHA256 over `token \n distinct_id \n request_id \n signed_at`,
// lowercase hex. See ai-gateway internal/emitter/signer.go.
function verifySignature(
    key: Buffer,
    token: string,
    distinctId: string,
    requestId: string,
    signedAt: string,
    signature: string
): boolean {
    const expected = createHmac('sha256', key)
        .update(`${token}\n${distinctId}\n${requestId}\n${signedAt}`)
        .digest('hex')
    const expectedBuf = Buffer.from(expected, 'hex')
    const actualBuf = Buffer.from(signature, 'hex')
    if (actualBuf.length === 0 || actualBuf.length !== expectedBuf.length) {
        return false
    }
    return timingSafeEqual(expectedBuf, actualBuf)
}

function isFresh(signedAt: string, capturedAt: Date | undefined, maxAgeMs: number): boolean {
    if (!capturedAt) {
        return false
    }
    const signedMs = Date.parse(signedAt)
    if (!Number.isFinite(signedMs)) {
        return false
    }
    // Compare against the server-stamped capture time (the `now` header), not
    // wall-clock now: ingestion lag must never make a real event look stale.
    // Symmetric window also absorbs the small capture/gateway clock skew.
    return Math.abs(capturedAt.getTime() - signedMs) <= maxAgeMs
}
