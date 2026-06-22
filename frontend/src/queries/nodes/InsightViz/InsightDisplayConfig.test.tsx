import '@testing-library/jest-dom'

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BindLogic, Provider } from 'kea'

import { FEATURE_FLAGS } from 'lib/constants'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'
import { insightDataLogic } from 'scenes/insights/insightDataLogic'
import { insightLogic } from 'scenes/insights/insightLogic'
import { insightVizDataLogic } from 'scenes/insights/insightVizDataLogic'

import { useMocks } from '~/mocks/jest'
import {
    InsightQueryNode,
    LifecycleQuery,
    NodeKind,
    StickinessQuery,
    TrendsQuery,
} from '~/queries/schema/schema-general'
import { initKeaTests } from '~/test/init'
import { BaseMathType, ChartDisplayType, InsightShortId } from '~/types'

import { InsightDisplayConfig } from './InsightDisplayConfig'

const Insight123 = '123' as InsightShortId
const insightProps = { dashboardItemId: Insight123 }

function makeTrendsQuery(
    display?: ChartDisplayType,
    trendsFilter: NonNullable<TrendsQuery['trendsFilter']> = {}
): TrendsQuery {
    return {
        kind: NodeKind.TrendsQuery,
        series: [
            {
                kind: NodeKind.EventsNode,
                name: '$pageview',
                event: '$pageview',
                math: BaseMathType.TotalCount,
            },
        ],
        trendsFilter: {
            display,
            ...trendsFilter,
        },
    }
}

const pageviewSeries = [
    {
        kind: NodeKind.EventsNode,
        name: '$pageview',
        event: '$pageview',
        math: BaseMathType.TotalCount,
    },
] as TrendsQuery['series']

function makeStickinessQuery(display?: ChartDisplayType): StickinessQuery {
    return {
        kind: NodeKind.StickinessQuery,
        series: pageviewSeries,
        stickinessFilter: { display },
    }
}

function makeLifecycleQuery(): LifecycleQuery {
    return {
        kind: NodeKind.LifecycleQuery,
        series: pageviewSeries,
        lifecycleFilter: { showLegend: true },
    }
}

async function openOptionsMenu(): Promise<void> {
    const optionsButtons = screen.getAllByRole('button', { name: /Options/ })
    await userEvent.click(optionsButtons[0])
}

function getDisplaySectionItems(): string[] {
    const displaySection = screen.getByTestId('options-display-section').closest('section')!
    const listItems = within(displaySection).getAllByRole('listitem')
    return listItems.map((li) => li.textContent?.trim() || '')
}

describe('InsightDisplayConfig', () => {
    beforeEach(() => {
        useMocks({
            get: {
                '/api/environments/:team_id/insights/trend': [],
                '/api/environments/:team_id/insights/': { results: [{}] },
            },
        })
        initKeaTests()
        featureFlagLogic().mount()
    })

    afterEach(() => {
        cleanup()
    })

    function setupAndRender(query: InsightQueryNode): void {
        insightLogic(insightProps).mount()
        insightDataLogic(insightProps).mount()
        const vizDataLogic = insightVizDataLogic(insightProps)
        vizDataLogic.mount()
        vizDataLogic.actions.updateQuerySource(query)

        render(
            <Provider>
                <BindLogic logic={insightLogic} props={insightProps}>
                    <InsightDisplayConfig />
                </BindLogic>
            </Provider>
        )
    }

    describe('box plot display options', () => {
        it('only shows "Show legend" in the Display section', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.BoxPlot))
            await openOptionsMenu()

            const items = getDisplaySectionItems()
            expect(items).toEqual(['Show legend', 'Exclude outliers'])
        })

        it('shows unit picker and Y-axis scale but not statistical analysis', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.BoxPlot))
            await openOptionsMenu()

            expect(screen.getByText('Y-axis scale')).toBeInTheDocument()
            expect(screen.queryByText('Statistical analysis')).not.toBeInTheDocument()
        })
    })

    describe('slope graph display options', () => {
        it('shows the "group by time period" interval picker — grouping defines the slope', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.SlopeGraph))
            expect(screen.getByText(/grouped/i)).toBeInTheDocument()
        })

        it('hides the compare picker', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.SlopeGraph))
            expect(screen.queryByText(/Compare to|Previous period/i)).not.toBeInTheDocument()
        })

        it('shows only the legend in the Display section', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.SlopeGraph))
            await openOptionsMenu()

            const items = getDisplaySectionItems()
            expect(items).toEqual(['Show legend'])
            // None of the time-series-only options should leak in.
            expect(items).not.toContain('Show values on series')
            expect(items).not.toContain('Show trend lines')
            expect(items).not.toContain('Show alert threshold lines')
            expect(items).not.toContain('Show multiple Y-axes')
            expect(items).not.toContain('Show annotations')
        })

        it('hides the Y-axis scale and statistical analysis sections', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.SlopeGraph))
            await openOptionsMenu()

            expect(screen.queryByText('Y-axis scale')).not.toBeInTheDocument()
            expect(screen.queryByText('Statistical analysis')).not.toBeInTheDocument()
        })
    })

    describe('line graph display options', () => {
        it('shows the "group by time period" interval picker (control for the slope graph)', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsLineGraph))
            expect(screen.getByText(/grouped/i)).toBeInTheDocument()
        })

        it('shows multiple options in the Display section', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsLineGraph))
            await openOptionsMenu()

            const items = getDisplaySectionItems()
            expect(items).toContain('Show legend')
            expect(items).toContain('Show values on series')
            expect(items).toContain('Show alert threshold lines')
            expect(items).toContain('Show trend lines')
        })

        it('shows Y-axis scale section', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsLineGraph))
            await openOptionsMenu()

            expect(screen.getByText('Y-axis scale')).toBeInTheDocument()
        })

        it('removes axis label option count after clearing a committed label', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsLineGraph, { xAxisLabel: 'Signup date' }))

            const optionsButton = screen.getAllByRole('button', { name: /Options/ })[0]
            expect(optionsButton).toHaveTextContent(/\(1\)/)

            await openOptionsMenu()
            const input = await screen.findByPlaceholderText('X-axis label')
            await userEvent.clear(input)
            expect(optionsButton).toHaveTextContent(/\(1\)/)

            fireEvent.blur(input)
            await waitFor(() => {
                expect(optionsButton).not.toHaveTextContent(/\(1\)/)
            })
        })
    })

    describe('line graph display options with the quill legend flag', () => {
        beforeEach(() => {
            featureFlagLogic.actions.setFeatureFlags([], {
                [FEATURE_FLAGS.PRODUCT_ANALYTICS_QUILL_LEGEND]: true,
            })
        })

        it('keeps the "Show legend" checkbox and adds a position select on the same row', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsLineGraph))
            await openOptionsMenu()

            const legendItem = getDisplaySectionItems().find((item) => item.includes('Show legend'))
            expect(legendItem).toBeTruthy()
            expect(legendItem).toContain('Bottom')
        })

        it.each([
            ['trends bar', () => makeTrendsQuery(ChartDisplayType.ActionsBar)],
            ['trends unstacked bar', () => makeTrendsQuery(ChartDisplayType.ActionsUnstackedBar)],
            ['stickiness line', () => makeStickinessQuery(ChartDisplayType.ActionsLineGraph)],
            ['stickiness bar', () => makeStickinessQuery(ChartDisplayType.ActionsBar)],
            ['lifecycle', () => makeLifecycleQuery()],
        ])('adds the legend position select for %s', async (_desc, makeQuery) => {
            setupAndRender(makeQuery())
            await openOptionsMenu()

            const legendItem = getDisplaySectionItems().find((item) => item.includes('Show legend'))
            expect(legendItem).toBeTruthy()
            expect(legendItem).toContain('Bottom')
        })

        it('keeps the plain "Show legend" checkbox for the aggregated bar-value chart', async () => {
            setupAndRender(makeTrendsQuery(ChartDisplayType.ActionsBarValue))
            await openOptionsMenu()

            // The aggregated bar-value layout has no in-chart legend, so it must not get a position select.
            const items = getDisplaySectionItems()
            expect(items.some((item) => item.includes('Bottom'))).toBe(false)
        })
    })
})
