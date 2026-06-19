import { createHmac, timingSafeEqual } from 'node:crypto'

import { PluginEvent } from '~/plugin-scaffold'

import { EventHeaders } from '../../../../types'
import { ok } from '../../../pipelines/results'
import { ProcessingStep } from '../../../pipelines/steps'

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
// is treated as unverified and stripped). maxAgeMs bounds how old a signed
// timestamp may be, which is what defends against replaying a captured
// signature; per-request dedup against the settlement ledger is a
// follow-up that closes the within-window replay gap.
export function createVerifyGatewayProvenanceStep<TInput extends VerifyGatewayProvenanceInput>(
    secret: string,
    maxAgeMs: number
): ProcessingStep<TInput, TInput> {
    const key = Buffer.from(secret)

    return function verifyGatewayProvenanceStep(input) {
        const properties = input.normalizedEvent.properties ?? {}
        const gatewayKeys = Object.keys(properties).filter((k) => k.startsWith(GATEWAY_PREFIX))
        if (gatewayKeys.length === 0) {
            return Promise.resolve(ok(input))
        }

        if (
            secret !== '' &&
            isTrusted(key, input.headers.token, input.normalizedEvent.distinct_id, properties, maxAgeMs)
        ) {
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
        return Promise.resolve(ok(input))
    }
}

function isTrusted(
    key: Buffer,
    token: string | undefined,
    distinctId: string,
    properties: Record<string, any>,
    maxAgeMs: number
): boolean {
    const signature = properties[SIGNATURE_PROPERTY]
    const signedAt = properties[SIGNED_AT_PROPERTY]
    if (typeof token !== 'string' || typeof signature !== 'string' || typeof signedAt !== 'string') {
        return false
    }
    const requestId = typeof properties[REQUEST_ID_PROPERTY] === 'string' ? properties[REQUEST_ID_PROPERTY] : ''
    return verifySignature(key, token, distinctId, requestId, signedAt, signature) && isFresh(signedAt, maxAgeMs)
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

function isFresh(signedAt: string, maxAgeMs: number): boolean {
    const signedMs = Date.parse(signedAt)
    if (!Number.isFinite(signedMs)) {
        return false
    }
    // Symmetric window absorbs clock skew in either direction.
    return Math.abs(Date.now() - signedMs) <= maxAgeMs
}
