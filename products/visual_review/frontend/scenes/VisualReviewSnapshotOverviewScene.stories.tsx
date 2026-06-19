import type { Meta, StoryObj } from '@storybook/react'

import { App } from 'scenes/App'

import { mswDecorator } from '~/mocks/browser'

import type { BaselineEntryApi, BaselineOverviewApi, RepoApi } from '../generated/api.schemas'

const REPO_ID = '00000000-0000-0000-0000-0000000000bb'

const repo: RepoApi = {
    id: REPO_ID,
    team_id: 1,
    repo_external_id: 99999,
    repo_full_name: 'PostHog/posthog',
    baseline_file_paths: {},
    enable_pr_comments: true,
    created_at: '2026-06-10T00:00:00Z',
}

// thumbnail_hash null keeps every card on its "No thumbnail" placeholder, so the
// grid renders deterministically without any image network round-trips.
const entry = (identifier: string, overrides: Partial<BaselineEntryApi> = {}): BaselineEntryApi => ({
    identifier,
    run_type: 'storybook',
    browser: null,
    thumbnail_hash: null,
    width: 320,
    height: 200,
    tolerate_count_30d: 0,
    tolerate_count_90d: 0,
    is_quarantined: false,
    last_run_at: '2026-06-10T00:00:00Z',
    baseline_change_count: 0,
    recent_drift_avg: null,
    ...overrides,
})

const entries: BaselineEntryApi[] = [
    entry('Components/Button--primary', { recent_drift_avg: 3.2, baseline_change_count: 4 }),
    entry('Components/Button--secondary', { tolerate_count_30d: 2, tolerate_count_90d: 5 }),
    entry('Components/Banner--info'),
    entry('Components/Banner--warning', { is_quarantined: true }),
    entry('Components/Modal--default', { recent_drift_avg: 1.1 }),
    entry('Components/Table--dense'),
    entry('Components/Tabs--scrollable'),
    entry('Components/Tooltip--top'),
    entry('Scenes/Dashboard--list', { run_type: 'playwright', browser: 'chromium' }),
    entry('Scenes/Insight--trends', { run_type: 'playwright', browser: 'chromium', recent_drift_avg: 0.4 }),
]

const overview: BaselineOverviewApi = {
    entries,
    totals: {
        by_run_type: { storybook: 8, playwright: 2 },
        all_snapshots: entries.length,
        recently_tolerated: 1,
        frequently_tolerated: 0,
        currently_quarantined: 1,
    },
    truncated: false,
    generated_at: '2026-06-10T00:00:00Z',
}

const meta: Meta = {
    component: App,
    title: 'Scenes-App/Visual review/Snapshot overview',
    parameters: {
        layout: 'fullscreen',
        viewMode: 'story',
        mockDate: '2026-06-10',
        pageUrl: `/visual_review/repos/${REPO_ID}/snapshots`,
        testOptions: {
            waitForSelector: '[data-attr="visual-review-snapshot-card"]',
            // narrow (568px) sits below the `sm` breakpoint, so the facet sidebar
            // stacks above the grid and the cards collapse to a single column;
            // wide (1300px) shows the side-by-side desktop layout.
            viewportWidths: ['narrow', 'wide'],
        },
    },
    decorators: [
        mswDecorator({
            get: {
                [`/api/projects/:team_id/visual_review/repos/${REPO_ID}/`]: repo,
                [`/api/projects/:team_id/visual_review/repos/${REPO_ID}/baselines/`]: overview,
            },
        }),
    ],
}
export default meta

export const Default: StoryObj = {}
