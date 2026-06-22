import { actions, connect, events, kea, listeners, path, selectors } from 'kea'
import { loaders } from 'kea-loaders'

import { lemonToast } from '@posthog/lemon-ui'

import api from 'lib/api'
import { FEATURE_FLAGS } from 'lib/constants'
import { integrationsLogic } from 'lib/integrations/integrationsLogic'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'

import type { personalIntegrationsLogicType } from './personalIntegrationsLogicType'

export interface PersonalGitHubIntegration {
    kind: string
    installation_id: string | null
    repository_selection: string | null
    account: { type: string; name: string } | null
    uses_shared_installation: boolean
    created_at: string | null
}

export interface PersonalSlackIntegration {
    id: string
    kind: 'slack'
    slack_user_id: string
    slack_team_id: string
    slack_team_name: string | null
    slack_email_at_link: string | null
    created_at: string | null
}

export interface LinkableSlackWorkspace {
    posthog_team_id: number
    posthog_team_name: string
    posthog_organization_name: string
    slack_team_id: string
    slack_team_name: string | null
}

interface GithubStartResponse {
    install_url: string
}

interface SlackStartResponse {
    install_url: string
}

/** Key for stashing the ``connect_from`` URL param across the GitHub install roundtrip.
 *
 * The install flow leaves posthog.com for github.com and comes back, which drops the query
 * string that brought the user here. sessionStorage survives the roundtrip because it's
 * scoped to the tab, not the navigation. */
const CONNECT_FROM_STORAGE_KEY = 'personal_integrations_connect_from'

function readConnectFromStorage(): string | null {
    try {
        return sessionStorage.getItem(CONNECT_FROM_STORAGE_KEY)
    } catch {
        return null
    }
}

function writeConnectFromStorage(value: string | null): void {
    try {
        if (value) {
            sessionStorage.setItem(CONNECT_FROM_STORAGE_KEY, value)
        } else {
            sessionStorage.removeItem(CONNECT_FROM_STORAGE_KEY)
        }
    } catch {
        console.warn('Failed to write connect_from value for account linking redirect, skipping', value)
    }
}

export const personalIntegrationsLogic = kea<personalIntegrationsLogicType>([
    path(['scenes', 'settings', 'user', 'personalIntegrationsLogic']),

    connect(() => ({
        actions: [
            integrationsLogic,
            ['loadIntegrations as loadProjectIntegrations', 'loadIntegrationsSuccess as projectIntegrationsLoaded'],
        ],
        values: [featureFlagLogic, ['featureFlags']],
    })),

    actions({
        connectGitHub: true,
        disconnectGitHub: (installationId: string) => ({ installationId }),
        disconnectSlack: (slackUserId: string) => ({ slackUserId }),
        // `connectSlack` is auto-defined by the slackConnect loader below.
        // Its payload is `{ workspace? }` so the picker can target a specific
        // Slack workspace; when omitted the backend falls back to the user's
        // current team's first Slack integration (one-workspace simple case).
    }),

    loaders(() => ({
        integrations: [
            [] as PersonalGitHubIntegration[],
            {
                loadIntegrations: async () => {
                    const response = await api.get<{ results: PersonalGitHubIntegration[] }>(
                        'api/users/@me/integrations/'
                    )
                    return response.results
                },
            },
        ],
        slackIntegrations: [
            [] as PersonalSlackIntegration[],
            {
                loadSlackIntegrations: async () => {
                    const response = await api.get<{ results: PersonalSlackIntegration[] }>(
                        'api/users/@me/integrations/slack/'
                    )
                    return response.results
                },
            },
        ],
        linkableSlackWorkspaces: [
            [] as LinkableSlackWorkspace[],
            {
                loadLinkableSlackWorkspaces: async () => {
                    const response = await api.get<{ results: LinkableSlackWorkspace[] }>(
                        'api/users/@me/integrations/slack/linkable_workspaces/'
                    )
                    return response.results
                },
            },
        ],
        // Trades a manual action+reducer pair for `connectSlack` (auto-defined
        // by the loader) plus `slackConnectLoading` (kea-loaders convention),
        // which the button reads for its spinner. The return value is unused;
        // success takes the tab off this page via window.location.
        slackConnect: [
            false as boolean,
            {
                connectSlack: async (payload: { workspace?: LinkableSlackWorkspace } = {}) => {
                    try {
                        const body = payload.workspace
                            ? {
                                  team_id: payload.workspace.posthog_team_id,
                                  slack_team_id: payload.workspace.slack_team_id,
                              }
                            : {}
                        const response = await api.create<SlackStartResponse>(
                            'api/users/@me/integrations/slack/start/',
                            body
                        )
                        window.location.href = response.install_url
                        return true
                    } catch (error: unknown) {
                        const message = error instanceof Error && 'detail' in error ? (error as any).detail : undefined
                        lemonToast.error(message || 'Could not start Slack linking.')
                        return false
                    }
                },
            },
        ],
    })),

    selectors({
        // Gating selector for the new section. Read once in the component so the
        // backend endpoints are still queryable for users who linked before the
        // flag flipped off — only the *new connect / discoverability* surface
        // is hidden, not the unlink path.
        slackLinkEnabled: [
            (s) => [s.featureFlags],
            (featureFlags): boolean => !!featureFlags[FEATURE_FLAGS.SLACK_USER_LINK],
        ],
    }),

    listeners(({ actions }) => ({
        projectIntegrationsLoaded: () => {
            // When a project-level integration is added/removed, the backend may
            // auto-create a user-level integration. Reload to pick it up.
            actions.loadIntegrations()
        },
        disconnectSlack: async ({ slackUserId }) => {
            try {
                await api.delete(`api/users/@me/integrations/slack/${encodeURIComponent(slackUserId)}/`)
                lemonToast.success('Unlinked your Slack account')
                actions.loadSlackIntegrations()
                // Refresh linkable so the just-unlinked workspace re-appears
                // in the connect picker without a page reload.
                actions.loadLinkableSlackWorkspaces()
            } catch {
                lemonToast.error('Could not unlink your Slack account.')
            }
        },
        connectGitHub: async () => {
            try {
                const connectFrom = readConnectFromStorage()
                const body = connectFrom === 'posthog_code' ? { connect_from: 'posthog_code' as const } : {}
                const response = await api.create<GithubStartResponse>('api/users/@me/integrations/github/start/', body)
                window.location.href = response.install_url
            } catch (error: unknown) {
                const message = error instanceof Error && 'detail' in error ? (error as any).detail : undefined
                lemonToast.error(message || 'Could not start GitHub installation.')
            }
        },
        disconnectGitHub: async ({ installationId }) => {
            try {
                await api.delete(`api/users/@me/integrations/github/${installationId}/`)
                lemonToast.success('Disconnected GitHub installation')
                actions.loadIntegrations()
                actions.loadProjectIntegrations()
            } catch {
                lemonToast.error('Could not disconnect GitHub installation.')
            }
        },
    })),

    events(({ actions }) => ({
        afterMount: () => {
            actions.loadIntegrations()
            actions.loadSlackIntegrations()
            actions.loadLinkableSlackWorkspaces()
            const params = new URLSearchParams(window.location.search)

            // Stash ``connect_from`` so the post-roundtrip success toast can surface a
            // "Return to PostHog Code" CTA.
            const connectFrom = params.get('connect_from')
            if (connectFrom) {
                writeConnectFromStorage(connectFrom)
            }

            if (params.has('github_link_success')) {
                writeConnectFromStorage(null)
                lemonToast.success('GitHub connected.')
            } else if (params.has('github_link_error')) {
                writeConnectFromStorage(null)
                const reason = params.get('github_link_error')
                const message =
                    reason === 'access_denied'
                        ? 'GitHub authorization was canceled.'
                        : reason === 'github_oauth_error'
                          ? 'GitHub rejected the authorization. Please try again.'
                          : reason === 'missing_params'
                            ? "GitHub didn't send back the expected parameters. Please try again."
                            : reason === 'invalid_state'
                              ? 'The GitHub link request expired or could not be verified. Please try again.'
                              : reason === 'exchange_failed'
                                ? 'GitHub rejected the authorization code. Check that the GitHub App is configured correctly.'
                                : reason === 'installation_fetch_failed'
                                  ? 'Could not fetch installation details from GitHub. Please try again.'
                                  : reason === 'installation_token_failed'
                                    ? 'Could not get an installation token from GitHub. Please try again.'
                                    : 'Could not connect GitHub. Please try again.'
                lemonToast.error(message)
            }
        },
    })),
])
