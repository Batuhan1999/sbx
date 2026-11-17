import { useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query'
import { api } from './endpoints'
import type { Sandbox, SandboxScope } from './types'
import { isBusy } from '@/lib/status'

export const keys = {
  me: ['me'] as const,
  sandboxes: ['sandboxes'] as const,
  sandboxList: (scope: SandboxScope, repo: string) => ['sandboxes', 'list', scope, repo] as const,
  sandbox: (name: string) => ['sandboxes', 'detail', name] as const,
  resources: (name: string) => ['sandboxes', 'resources', name] as const,
  repos: ['repos'] as const,
  repo: (repo: string) => ['repo', repo] as const,
  repoInfo: (repo: string) => ['repo', repo, 'info'] as const,
  readiness: (repo: string) => ['repo', repo, 'readiness'] as const,
  env: (repo: string) => ['repo', repo, 'env'] as const,
  files: (repo: string) => ['repo', repo, 'files'] as const,
  template: (repo: string) => ['repo', repo, 'template'] as const,
  brief: (repo: string) => ['repo', repo, 'brief'] as const,
  setupPr: (repo: string) => ['repo', repo, 'setup-pr'] as const,
  github: ['github-repositories'] as const,
  catalog: ['catalog'] as const,
  catalogEntry: (name: string) => ['catalog', name] as const,
  sharedConnections: ['catalog-connections'] as const,
  connections: ['connections'] as const,
  setup: ['setup'] as const,
  setupChecks: ['setup', 'checks'] as const,
  settings: ['settings'] as const,
  updates: ['updates'] as const,
  runtimes: ['runtimes'] as const,
  runtimesAvailable: ['runtimes', 'available'] as const,
  runtimeChange: ['runtimes', 'change'] as const,
  endpoints: ['endpoints'] as const,
}

const anyBusy = (list?: Sandbox[]) => !!list?.some(isBusy)

export const useMe = () => useQuery({ queryKey: keys.me, queryFn: api.me, staleTime: 60_000, retry: false })

/** Newer releases and the last upgrade, for admins; every 3 s while an upgrade
 * runs, through sbx-control's restart (a failed poll keeps the last answer). */
export const useUpdates = (enabled: boolean) => useQuery({
  queryKey: keys.updates, queryFn: api.updates.get, enabled, staleTime: 60_000, retry: false,
  refetchInterval: q => (q.state.data?.upgrade?.state === 'running' ? 3_000 : 10 * 60_000),
})

/** The agents this server offers, from /api/me; none while it loads. */
export const useAgents = () => Object.values(useMe().data?.agents ?? {})

export const useSandboxes = (scope: SandboxScope, repo = '', enabled = true) => useQuery({
  queryKey: keys.sandboxList(scope, repo),
  queryFn: () => api.sandboxes.list(scope, repo),
  enabled,
  refetchInterval: q => (anyBusy(q.state.data?.sandboxes) ? 3_000 : 20_000),
})

export const useSandbox = (name: string) => useQuery({
  queryKey: keys.sandbox(name),
  queryFn: () => api.sandboxes.detail(name),
  refetchInterval: q => (q.state.data && isBusy(q.state.data) ? 3_000 : 15_000),
})

export const useResources = (name: string, enabled = true) => useQuery({
  queryKey: keys.resources(name), queryFn: () => api.sandboxes.resources(name), enabled, staleTime: 30_000, retry: false,
})

export const useRuntimes = () => useQuery({ queryKey: keys.runtimes, queryFn: api.runtimes, staleTime: 60_000, retry: false })
/** The enabled runtimes when there is a choice between them; null with one (or while loading), and the UI then says nothing about runtimes. */
export function useRuntimeChoice() {
  const data = useRuntimes().data
  return data && data.runtimes.length > 1 ? data : null
}
/** With a choice of runtimes, a runtime's title by kind (the kind when it is not enabled); null with one. */
export function useRuntimeTitle() {
  const choice = useRuntimeChoice()
  return choice ? (kind?: string) => choice.runtimes.find(r => r.kind === kind)?.title ?? kind ?? '' : null
}

export const useAvailableRuntimes = (enabled = true) => useQuery({ queryKey: keys.runtimesAvailable, queryFn: api.runtimeSettings.available, enabled, retry: false })

/** Queued, applying, or done but answered by the process from before the restart that takes it. */
const pending = (c?: { state: string; restarting?: boolean }) => c?.state === 'queued' || c?.state === 'applying' || (c?.state === 'done' && !!c.restarting)
/** The last runtime change; every 2 s until it is done and a restarted sbx-control answers
 * (a failed poll keeps the last answer, so polling goes on through the restart). */
export const useRuntimeChange = (enabled = true) => useQuery({
  queryKey: keys.runtimeChange, queryFn: api.runtimeSettings.change, enabled, retry: false,
  refetchInterval: q => (pending(q.state.data) ? 2_000 : false),
})
export const isApplying = (change?: { state: string; restarting?: boolean }) => pending(change)

/** What provider sandboxes reach, for admins; every 2 s while one is applying (a failed poll keeps the last answer). */
export const useEndpoints = (enabled = true) => useQuery({
  queryKey: keys.endpoints, queryFn: api.endpoints.list, enabled, retry: false,
  refetchInterval: q => (q.state.data?.endpoints.some(e => e.state === 'applying') ? 2_000 : false),
})

export const useRepos = () => useQuery({ queryKey: keys.repos, queryFn: () => api.repos.list().then(d => d.repos) })
export const useRepoInfo = (repo: string, enabled = true) => useQuery({ queryKey: keys.repoInfo(repo), queryFn: () => api.repos.get(repo), enabled: enabled && !!repo, retry: false })
export const useReadiness = (repo: string) => useQuery({ queryKey: keys.readiness(repo), queryFn: () => api.repos.readiness(repo), refetchInterval: 10_000 })
export const useEnv = (repo: string, enabled = true) => useQuery({ queryKey: keys.env(repo), queryFn: () => api.env.view(repo), enabled, retry: false })
export const useRecipeFiles = (repo: string) => useQuery({ queryKey: keys.files(repo), queryFn: () => api.repos.files(repo) })
export const useTemplate = (repo: string) => useQuery({
  queryKey: keys.template(repo),
  queryFn: () => api.repos.template(repo),
  refetchInterval: q => (q.state.data?.status === 'building' ? 4_000 : false),
})
export const useSetupPr = (repo: string, enabled: boolean) => useQuery({
  queryKey: keys.setupPr(repo), queryFn: () => api.repos.setupPr(repo), enabled, retry: false, staleTime: 60_000,
})
export const useGitHubRepositories = (enabled: boolean) => useQuery({
  queryKey: keys.github, queryFn: () => api.repos.github().then(d => d.repositories), enabled, retry: false, staleTime: 60_000,
})

export const useCatalog = () => useQuery({ queryKey: keys.catalog, queryFn: api.catalog.list, staleTime: 60_000, select: d => d.packages })
export const useCatalogProblems = (enabled = true) => useQuery({ queryKey: keys.catalog, queryFn: api.catalog.list, staleTime: 60_000, enabled, select: d => d.problems })
export const useCatalogEntry = (name: string | null) => useQuery({
  queryKey: keys.catalogEntry(name ?? ''), queryFn: () => api.catalog.entry(name!), enabled: !!name, staleTime: 60_000,
})
export const useSharedConnections = (enabled = true) => useQuery({
  queryKey: keys.sharedConnections, queryFn: () => api.catalog.connections().then(d => d.connections), enabled,
})
export const useConnections = (enabled = true) => useQuery({ queryKey: keys.connections, queryFn: () => api.connections.list().then(d => d.connections), enabled })

export const useSetup = () => useQuery({
  queryKey: keys.setup, queryFn: api.setup.state, retry: false,
  refetchInterval: q => (q.state.data?.complete ? false : 4_000),
})
export const useSetupChecks = (enabled = true) => useQuery({ queryKey: keys.setupChecks, queryFn: api.setup.checks, enabled, retry: false, staleTime: 15_000 })
export const useSettings = (enabled = true) => useQuery({ queryKey: keys.settings, queryFn: api.settings.get, enabled })

export function useAction<TIn = void, TOut = unknown>(run: (input: TIn) => Promise<TOut>, ...refresh: QueryKey[]) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: run,
    onSuccess: () => Promise.all(refresh.map(queryKey => qc.invalidateQueries({ queryKey }))),
  })
}
