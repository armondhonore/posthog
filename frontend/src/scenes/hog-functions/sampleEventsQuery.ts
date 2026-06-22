import { dayjs } from 'lib/dayjs'

import { performQuery } from '~/queries/query'
import { EventsQuery, EventsQueryResponse } from '~/queries/schema/schema-general'

// Two-phase EventsQuery to keep memory bounded on high-volume teams.
//
// Phase 1 keeps the original filter but projects only `uuid, timestamp` — wide JSON columns
// (`properties`, `person_properties`, `groupN_properties`, `elements_chain`) aren't decompressed.
//
// Phase 2 hydrates by exact `(uuid, timestamp)` tuples and drops the original filter. Phase 1
// already certified the matches, so re-applying the filter would force ClickHouse to re-read
// `properties` for filter evaluation across the same granule set — and now also project the wide
// JSON columns on top, which is what trips the per-query memory limit. The tuple constraint lets
// the primary-key sparse index over `(team_id, toDate(timestamp), event, ...)` prune to just the
// granules holding those rows.
//
// On top of that, we try a ladder of progressively wider windows before the caller's full range.
// Most matching events the user wants to test against fire recently, so a high-volume team resolves
// at the first narrow step and never pays for the deep scan — and that deep scan is exactly what
// trips the per-query memory limit on massive teams. A low-volume team (which doesn't hit the memory
// ceiling) simply falls through each empty step to its original window.
const PRE_STAGE_WINDOWS = ['-1h', '-6h', '-24h', '-7d']

export async function performWideEventsQueryInTwoPhases(intent: EventsQuery): Promise<EventsQueryResponse> {
    for (const after of preStageWindows(intent.after)) {
        const preResponse = await runTwoPhase({ ...intent, after })
        if ((preResponse.results as unknown[]).length > 0) {
            return preResponse
        }
    }
    return await runTwoPhase(intent)
}

async function runTwoPhase(intent: EventsQuery): Promise<EventsQueryResponse> {
    const phaseOne: EventsQuery = {
        ...intent,
        select: ['uuid', 'timestamp'],
    }
    const phaseOneResponse = await performQuery(phaseOne)
    const phaseOneResults = phaseOneResponse.results as Array<[string, string]>
    if (phaseOneResults.length === 0) {
        return phaseOneResponse
    }

    const timestampsMs = phaseOneResults.map(([, t]) => dayjs(t).valueOf())
    const after = dayjs(Math.min(...timestampsMs))
        .subtract(1, 'second')
        .toISOString()
    const before = dayjs(Math.max(...timestampsMs))
        .add(1, 'second')
        .toISOString()

    const tupleList = phaseOneResults.map(([u, t]) => `('${u}', ${formatClickHouseUtcDateTime64(t)})`).join(', ')

    const phaseTwo: EventsQuery = {
        ...intent,
        fixedProperties: undefined,
        properties: undefined,
        where: [...(intent.where ?? []), `(uuid, timestamp) IN (${tupleList})`],
        after,
        before,
        limit: phaseOneResults.length,
    }

    return await performQuery(phaseTwo)
}

// dayjs only has millisecond precision, so lift the microseconds straight from the source string
// and reattach them after converting the base to UTC.
function formatClickHouseUtcDateTime64(timestamp: string): string {
    const microsMatch = timestamp.match(/\.(\d{1,6})/)
    const micros = (microsMatch?.[1] ?? '').padEnd(6, '0')
    const base = dayjs(timestamp).utc().format('YYYY-MM-DD HH:mm:ss')
    return `toDateTime64('${base}.${micros}', 6, 'UTC')`
}

// Pre-stage windows strictly narrower than the caller's window, narrowest first. We never widen a
// caller's request, so anything as wide as (or wider than) `after` is dropped — the caller's own
// window is the final fallback. An unbounded or unparseable `after` gets no pre-stages.
function preStageWindows(after: string | undefined): string[] {
    const callerHours = windowToHours(after)
    if (callerHours === null) {
        return []
    }
    return PRE_STAGE_WINDOWS.filter((window) => {
        const hours = windowToHours(window)
        return hours !== null && hours < callerHours
    })
}

// Width of a window in hours. Accepts the relative range shorthand the testing flows use (`-7d`,
// `-30d`, `-24h`, `-1h`, etc.) and absolute ISO timestamps. Returns null when there's no bound or
// the value can't be parsed.
function windowToHours(after: string | undefined): number | null {
    if (!after) {
        return null
    }
    const relativeMatch = after.match(/^-(\d+)([smhdwMy])$/)
    if (relativeMatch) {
        const value = parseInt(relativeMatch[1], 10)
        const unit = relativeMatch[2] as 's' | 'm' | 'h' | 'd' | 'w' | 'M' | 'y'
        return dayjs.duration(value, unit).asHours()
    }
    const parsed = dayjs(after)
    if (parsed.isValid()) {
        return dayjs().diff(parsed, 'hour', true)
    }
    return null
}
