import { GatewayNonceOutcome, GatewayNonceStore } from '~/ingestion/common/ai-subpipeline.contract'
import { RedisPool } from '~/types'
import { logger } from '~/utils/logger'

// Honors a signed tuple once per (token, request_id); a reuse within the
// freshness window is a replay.
export class RedisGatewayNonceStore implements GatewayNonceStore {
    constructor(private readonly redisPool: RedisPool) {}

    async markSeen(token: string, requestId: string, ttlMs: number): Promise<GatewayNonceOutcome> {
        const key = `gw:nonce:${token}:${requestId}`
        const ttlSeconds = Math.max(1, Math.ceil(ttlMs / 1000))
        let client
        try {
            client = await this.redisPool.acquire()
            // SET NX returns 'OK' when it set the key (first sighting) and null
            // when the key already existed (a replay of the same signed tuple).
            const res = await client.set(key, '1', 'EX', ttlSeconds, 'NX')
            return res === 'OK' ? 'first' : 'replay'
        } catch (error) {
            // Fail open: a Redis blip degrades to freshness-window-only protection
            // rather than stripping the verified marker off legitimate gateway
            // events, which would double-bill them against the AIO meter.
            logger.warn('🔏', '[GatewayNonceStore] dedup unavailable, failing open', { error: String(error) })
            return 'unavailable'
        } finally {
            if (client) {
                await this.redisPool.release(client)
            }
        }
    }
}
