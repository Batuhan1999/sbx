// Every route the web app calls, typed. Pages use these through queries.ts.
import { enc, postForm, repoPath as r, request } from './client'
import type {
  AttachBody, Attachment, AvailableRuntime, CatalogEntry, CatalogPackage, CheckResult, Connection, CreateSandbox, EnvChange, EnvView,
  RuntimeChange,
  GitHubRepository, Me, RecipeFiles, RepoDetail, RepoInspection, Runtimes, RepoReadiness, RepoSummary, Sandbox,
  SandboxDetail, SandboxInspection, SandboxList, SandboxResources, SandboxScope, SetVariable, Settings, SetupPr, SharedConnection,
  SetupState, TemplateState, Updates, Audience,
} from './types'

const get = <T,>(url: string) => request<T>('GET', url)
const post = <T,>(url: string, body?: unknown) => request<T>('POST', url, body)
const put = <T,>(url: string, body: unknown) => request<T>('PUT', url, body)
const del = <T,>(url: string) => request<T>('DELETE', url)
const s = (name: string) => `/api/sandboxes/${enc(name)}`

export const api = {
  me: () => get<Me>('/api/me'),
  runtimes: () => get<Runtimes>('/api/runtimes'),
  /** Admins (and the setup token): checked by the server, then applied by the host, which restarts sbx-control. */
  runtimeSettings: {
    available: () => get<AvailableRuntime[]>('/api/runtimes/available'),
    change: () => get<RuntimeChange>('/api/runtimes/change'),
    add: (kind: string, values: Record<string, string>) => post<RuntimeChange>('/api/runtimes', { kind, values }),
    /** A secret left out keeps its value. */
    update: (kind: string, values: Record<string, string>) => put<RuntimeChange>(`/api/runtimes/${enc(kind)}`, { values }),
    remove: (kind: string) => del<RuntimeChange>(`/api/runtimes/${enc(kind)}`),
    setDefault: (kind: string) => put<RuntimeChange>('/api/runtimes/default', { kind }),
  },
  signIn: (email: string, password: string) => postForm<{ next: string }>('/auth/login', { email, password }),

  sandboxes: {
    list: (scope: SandboxScope, repo = '') => get<SandboxList>(`/api/sandbox-list?scope=${enc(scope)}&repo=${enc(repo)}`),
    detail: (name: string) => get<SandboxDetail>(`${s(name)}/detail`),
    resources: (name: string) => get<SandboxResources>(`${s(name)}/resources`),
    /** Resumes a paused sandbox to read its logs: call on request only. */
    inspect: (name: string, lines = 200) => get<SandboxInspection>(`${s(name)}/inspect?lines=${lines}`),
    create: (body: CreateSandbox) => post<Sandbox>('/api/sandboxes', body),
    pause: (name: string) => post<unknown>(`${s(name)}/pause`),
    resume: (name: string) => post<unknown>(`${s(name)}/resume`),
    restart: (name: string) => post<unknown>(`${s(name)}/restart`),
    remove: (name: string) => del<{ deleted: string }>(s(name)),
  },

  repos: {
    list: () => get<{ repos: RepoSummary[] }>('/api/repos'),
    get: (repo: string) => get<RepoDetail>(r(repo)),
    /** Admins only; null goes back to the installation's default. */
    setRuntime: (repo: string, runtime: string | null) => put<RepoDetail>(`${r(repo)}/runtime`, { runtime }),
    readiness: (repo: string) => get<RepoReadiness>(`${r(repo)}/readiness`),
    setupBrief: (repo: string) => request<string>('GET', `${r(repo)}/setup-brief`, undefined, 'text'),
    files: (repo: string) => get<RecipeFiles>(`${r(repo)}/files`),
    template: (repo: string) => get<TemplateState>(`${r(repo)}/template`),
    buildTemplate: (repo: string) => post<{ repo: string; status: string }>(`${r(repo)}/template`),
    readRecipe: (repo: string) => post<{ configured: boolean; source: string }>(`${r(repo)}/prepare`),
    setupPr: (repo: string) => get<SetupPr>(`${r(repo)}/setup-pr`),
    github: () => get<{ repositories: GitHubRepository[] }>('/api/github/repositories'),
    inspect: (repo: string) => post<RepoInspection>('/api/repo-onboarding/inspect', { repo }),
    connect: (repo: string) => post<RepoInspection>('/api/repo-onboarding/connect', { repo }),
  },

  env: {
    view: (repo: string) => get<EnvView>(`${r(repo)}/env`),
    set: (repo: string, name: string, body: SetVariable) => put<{ name: string; secret: boolean } & EnvChange>(`${r(repo)}/env/${enc(name)}`, body),
    remove: (repo: string, name: string, mine: boolean) => del<{ name: string; deleted: boolean } & EnvChange>(`${r(repo)}/env/${enc(name)}?mine=${mine}&restart=true`),
    import: (repo: string, body: { text: string; mine: boolean; audience: Audience; agent: boolean; restart: boolean }) =>
      post<{ set: Record<string, boolean> } & EnvChange>(`${r(repo)}/env-import`, body),
  },

  attachments: {
    /** Admins only. */
    attach: (repo: string, body: AttachBody) => post<{ attachment: Attachment }>(`${r(repo)}/attachments`, body),
    detach: (repo: string, key: string) => del<{ key: string }>(`${r(repo)}/attachments/${enc(key)}`),
    check: (repo: string, key: string) => post<{ key: string; ok: boolean; detail: string }>(`${r(repo)}/attachments/${enc(key)}/check`),
  },

  catalog: {
    /** problems: operator packages that could not be read. */
    list: () => get<{ packages: CatalogPackage[]; problems: string[] }>('/api/catalog'),
    entry: (name: string) => get<CatalogEntry>(`/api/catalog/${enc(name)}`),
    // The installation's shared connections (admins only).
    connections: () => get<{ connections: SharedConnection[] }>('/api/catalog/connections'),
    connect: (body: { package: string; key: string; inputs: Record<string, string> }) => post<SharedConnection>('/api/catalog/connections', body),
    checkConnection: (key: string) => post<{ key: string; ok: boolean; detail: string }>(`/api/catalog/connections/${enc(key)}/check`),
    removeConnection: (key: string) => del<{ key: string; removed: boolean }>(`/api/catalog/connections/${enc(key)}`),
  },

  connections: {
    list: () => get<{ connections: Connection[] }>('/api/connections'),
    save: (kind: string, values: Record<string, string>) => put<Connection>(`/api/connections/${enc(kind)}`, { values }),
    test: (kind: string) => post<CheckResult>(`/api/connections/${enc(kind)}/test`),
    disconnect: (kind: string) => del<Connection>(`/api/connections/${enc(kind)}`),
    githubManifest: (owner: string, returnTo: '/app/repos/new' | '/app/extensions') =>
      post<{ action_url: string; manifest: string }>('/api/connections/github/manifest', { owner, return_to: returnTo }),
  },

  setup: {
    state: () => get<SetupState>('/api/setup'),
    checks: () => get<CheckResult>('/api/setup/checks'),
    address: (address: string) => post<SetupState>('/api/setup/address', { address }),
    admin: (email: string, password: string) => post<SetupState>('/api/setup/admin', { email, password }),
    complete: () => post<SetupState>('/api/setup/complete'),
  },

  updates: {
    get: () => get<Updates>('/api/updates'),
    /** Admins only; the tag must be the newest release, which the server checks again. */
    install: (tag: string) => post<Updates>('/api/updates', { tag }),
    check: () => post<Updates>('/api/updates/check'),
  },

  settings: {
    get: () => get<Settings>('/api/settings'),
    put: (section: string, value: unknown, revision: number) => put<{ revision: number }>(`/api/settings/${enc(section)}`, { value, revision }),
  },
}
