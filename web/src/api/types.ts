// Shapes the control service returns (control/sbx_control/*_api.py, env.py,
// catalog/, connections/, setup.py, lifecycle.py).
// Optional fields are ones the server leaves out in some states.

export type Me = {
  email: string
  admin: boolean
  login: 'oidc' | 'password' | 'none'
  domain: string
  base_url: string
  setup_complete: boolean
  version: string
  repos: string[]
  /** The agent connectors this server offers (extensions.logins()), by kind. */
  agents: Record<string, Agent>
}

/** A published release newer than the installed one (updates.py). */
/** A person of this server (GET /api/users, admins only). */
export type User = {
  email: string
  admin: boolean
  /** Whether they have a password of their own (password sign-in). */
  password: boolean
  created_at: string | null
  created_by: string | null
  last_sign_in_at: string | null
  tokens: number
}
export type Users = { users: User[]; login: Me['login'] }
/** A one-time link that sets a person's password. */
export type PasswordLink = { email: string; link: string; days: number }

export type Release = { tag: string; url: string; notes: string; breaking: boolean; prerelease: boolean; published_at: string }

/** One step of an upgrade: the download, then each step install.sh announces. */
export type UpgradeStep = { title: string; state: 'running' | 'done' | 'failed'; detail: string }

/** An upgrade the host ran or runs; rollback only while the new release is the active one. */
export type Upgrade = {
  tag: string; from: string; state: 'running' | 'done' | 'failed'; log: string
  error?: string; rollback?: string; started_at?: string; finished_at?: string
  /** Absent while the request waits for the host. */
  steps?: UpgradeStep[]
  /** 0..1, estimated from the steps install.sh announced. */
  progress?: number
  log_tail?: string[]
}

/** GET /api/updates, admins only. */
export type Updates = {
  installed: string
  check: boolean
  /** When GitHub was last asked (ISO), and what went wrong then. */
  checked_at: string
  check_error: string
  /** The newest release past the installed one, and every release in between, newest first. */
  available: { latest: string; url: string; breaking: boolean; releases: Release[] } | null
  upgrade: Upgrade | null
}

/** An agent connector: `sbx open NAME <command>` runs it inside a sandbox. */
export type Agent = { title: string; command: string }

// lifecycle.py: the one record of a sandbox's state.
export type LifecyclePhase =
  | 'queued' | 'allocating' | 'provisioning_services' | 'preparing_workspace' | 'workspace_ready'
  | 'services_ready' | 'setting_up' | 'starting_app' | 'restoring_runtime' | 'pausing' | 'deleting'
  | 'app_ready' | 'app_unverified' | 'failed' | 'interrupted' | 'deleted' | 'unknown'
export type LifecycleOperation = 'create' | 'prepare' | 'restart' | 'resume' | 'pause' | 'delete' | 'env_update'
export type Application = 'not_started' | 'setting_up' | 'starting_app' | 'healthy' | 'unverified' | 'failed'
export type SandboxStatusWord = 'creating' | 'starting' | 'paused' | 'up' | 'workspace_ready' | 'failed' | 'deleted' | 'unknown'
export type ReadinessWord = 'not_ready' | 'workspace_ready' | 'services_ready' | 'app_ready' | 'unknown'

export type Lifecycle = {
  revision: number
  phase: LifecyclePhase
  previous_phase?: LifecyclePhase
  operation: LifecycleOperation
  operation_id: string
  active: boolean
  runtime: string
  workspace_ready: boolean
  services_ready: boolean
  application: Application
  sandbox_id: string | null
  error: string | null
  updated_at?: string
  repository?: string | null
  readiness?: ReadinessWord
  status?: SandboxStatusWord
  workspace_usable?: boolean
}

export type Origin = { kind: 'pr' | 'session' | 'person'; label: string; href?: string; detail?: string }

/** What the person viewing may do now: the operations are the owner's, logs anyone who may view it. */
export type SandboxActions = { pause: boolean; resume: boolean; restart: boolean; remove: boolean; logs: boolean }

export type Sandbox = {
  name: string
  owner: string | null
  branch: string
  sandbox_id: string
  status: SandboxStatusWord
  readiness?: ReadinessWord
  lifecycle?: Lifecycle | null
  error: string | null
  urls: Record<string, string>
  preview_access?: { mode: 'domain' | 'ip' }
  /** What the sandbox's state allows the viewer now; the server decides. */
  actions: SandboxActions
  app: string
  /** The VM as the runtime reports it: running, paused; missing without one; "" before it exists. */
  vm: string
  created_at: string | null
  origin: Origin
  /** The kind of runtime it runs on (GET /api/runtimes names it). */
  runtime?: string
}

// The runtime's limits, by field, when it can't say how much room it has.
export type RuntimeLimits = { timeout_seconds?: number; cpu?: number; memory_mb?: number; disk_mb?: number; sandboxes?: number }
/** runtime: the kind of runtime the room is on. */
export type Capacity = { known: boolean; memory_mb?: number; slots_total?: number; slots_free?: number; free_mb?: number; total_mb?: number; limits?: RuntimeLimits; runtime?: string }
/** capacity: the default runtime's; capacities: each enabled runtime's by kind, only with several. */
export type SandboxList = { sandboxes: Sandbox[]; capacity: Capacity; capacities?: Record<string, Capacity> }
export type SandboxScope = 'mine' | 'previews' | 'sessions' | 'all'

export type SandboxService = { key: string; uses: string; status: string; kind: string; created_at: string | null; outputs: Record<string, string> }
/** internal: the server's own housekeeping (retried cleanups, self-repair); nobody acts on it. */
export type SandboxEvent = { at: string; kind: string; detail: string; internal: boolean }
export type SandboxPreview = { name: string; port: number; access: string; link: boolean; url: string }

export type SandboxDetail = Sandbox & {
  services: SandboxService[]
  events: SandboxEvent[]
  previews: SandboxPreview[]
  paused_days: number
}

export type SandboxResources = {
  name: string
  allocated: { cpu: number; memory_mb: number }
  recipe: { cpu?: number | null; memory_mb?: number | null; swap_mb?: number | null }
  issues: { code: string; message: string; resource?: string }[]
}

export type SandboxInspection = {
  name: string
  commit: string
  subject: string
  env: string[]
  logs: { path: string; text: string }[]
  lines: number
  skipped: string[]
}

/** runtime: a kind from GET /api/runtimes; left out, the repo's setting or the installation's default. */
export type CreateSandbox = { name: string; branch?: string; repo?: string; public_keys: string; runtime?: string }

/** An enabled sandbox runtime (GET /api/runtimes). host: it runs on this server, not at a provider. */
export type SandboxRuntime = {
  kind: string
  title: string
  default: boolean
  host: boolean
  capabilities: { snapshots: boolean; private_networks: boolean; egress_rules: boolean }
  limits: RuntimeLimits
  /** running: how many of this installation's sandboxes run there, when it says. */
  status: { state: ConnectionState | string; summary: string; running?: number | null }
  /** The last limit its provider refused, until its settings change. */
  limit_hit?: LimitHit | null
  /** The plan its limits match ("custom": none), for a runtime with plans; null otherwise. */
  plan?: RuntimePlan | null
}
export type RuntimePlan = { name: string; title: string }
/** default is "" and runtimes empty until a runtime is set up. */
export type Runtimes = { default: string; runtimes: SandboxRuntime[] }

export type LimitHit = { field: string; asked: number | null; allowed: number | null; message: string; setting: string; at: string }
/** One of a runtime's settings: a key of runtime.env. A secret is write-only. limit: the limit an int setting sets. */
export type RuntimeSetting = { key: string; label: string; secret: boolean; default: string; help: string; kind: 'text' | 'int' | 'hostname'; optional: boolean; limit: string; rekey?: boolean
  /** A hostname's pattern, the server's own (runtime.base.HOSTNAME). */
  pattern?: string
  /** An int's range and step, in its unit ("seconds", "MiB", or "" for a count). */
  min?: number | null; max?: number | null; step?: number | null; unit?: string }
/** adjustable: keys the plan does not fix (no published number), set from values; note says why. */
export type RuntimePreset = { name: string; title: string; values: Record<string, string>; source: string; adjustable?: string[]; note?: string }
/** A runtime an installed package provides, enabled or not (GET /api/runtimes/available, admins). */
export type AvailableRuntime = {
  kind: string
  title: string
  host: boolean
  enabled: boolean
  default: boolean
  can_run: boolean
  why_not: string
  /** "install.sh" for a runtime on this host. */
  managed_by: string
  /** Why the web app can't add or change it now; null when it can. */
  edit_blocker: Blocker | null
  settings: RuntimeSetting[]
  presets: RuntimePreset[]
  values: Record<string, string>
  secrets_set: string[]
  facts: { kvm?: boolean; memory_gb?: number; disk_free_gb?: number }
  /** Its sandboxes' names when enabled; null when it did not say. */
  sandboxes: string[] | null
  /** Why it can't be removed now; null when it can. */
  remove_blocker: Blocker | null
  limit_hit: LimitHit | null
  /** An enabled one's plan now, as GET /api/runtimes says it. */
  plan?: RuntimePlan | null
}
/** Why a runtime can't be edited or removed now: code to branch on, message for people, the sandboxes in the way. */
export type Blocker = {
  code: 'host' | 'local_store' | 'needs_domain' | 'last_runtime' | 'unlisted' | 'has_sandboxes'
  message: string
  sandboxes: string[]
}
export type HostRequestState = 'idle' | 'queued' | 'applying' | 'done' | 'failed'
/** A request the host applies as root (the server's host_requests): queued, applying, then done or failed.
 * restarting: done, and sbx-control's restart that takes it is still to come (the answer is from the process before it). */
export type HostRequestStatus = { state: HostRequestState; message: string; at: string; restarting?: boolean }
/** unchanged: done at once, nothing to apply (the default already). */
export type RuntimeChange = HostRequestStatus & { operation: '' | 'add' | 'change' | 'remove' | 'default' | 'batch'; kind: string; unchanged?: boolean }
/** What a runtime's form sends: a plan (its limits resolved by the server), or none, and the values set. */
export type RuntimeValues = { plan?: string; values: Record<string, string> }

export type Trust = 'trusted' | 'public' | null
/** Who has to act: the coding agent, a person, an admin, or nobody (the server is working on it). */
export type Actor = 'agent' | 'person' | 'admin' | 'platform'
/** How a state reads at a glance (the UI's tones). */
export type Level = 'ok' | 'warn' | 'bad' | 'progress' | 'idle'

export type RepoSummary = {
  repo: string
  trust: Trust
  configured: boolean
  prebuilt: boolean
  built_at: string | null
  sandboxes: number
  /** What the repo needs next, as the list shows it. */
  state: { label: string; who?: Actor; level: Level }
}

export type RepoInfo = {
  repo: string
  trust: Trust
  configured: boolean
  prebuilt: boolean
  previews: string[]
  secrets_missing: string[]
  template_build: string
  template_stale: boolean
  github: 'app' | 'key' | 'anonymous' | '-'
  github_app_install: string
  github_anonymous: boolean
  /** server: the operator's copy (sbx recipe apply); repository: imported from the default branch. */
  recipe_source?: 'server' | 'repository' | ''
  /** The recipe's pull request as last recorded; current: it carries the recipe the server runs. */
  recipe_pr?: RecipePr | null
}

export type RecipePr = {
  number: number | null
  url: string | null
  state: 'open' | 'merged' | 'closed' | 'in_repository' | null
  draft: boolean
  recipe_sha256: string
  /** Why the last try could not publish it (GitHub access), when it failed. */
  problem: string | null
  current?: boolean
}

/** GET /api/repos/{repo}: runtime is the repo's own setting, null for the installation's default. */
export type RepoDetail = RepoInfo & { runtime?: string | null }

export type ReadinessStepId = 'served' | 'github' | 'configured' | 'template' | 'services' | 'env' | 'sandbox' | 'recipe_pr'
export type ReadinessStepStatus = 'done' | 'todo' | 'working' | 'blocked'
export type ReadinessStep = {
  step: ReadinessStepId
  status: ReadinessStepStatus
  /** Who unblocks it, when it is not done. */
  who?: Actor
  /** Names of what is missing: env variables, attachment keys. */
  missing: string[]
  detail: string
  /** The github and recipe_pr steps: the App's install URL while it is not done. */
  action?: string
  /** The owner's how-to page for the step, if it has one. */
  help_url?: string
  /** The recipe_pr step: the pull request. */
  url?: string
}
/** agent: hand it to the coding agent; person: someone must enter or approve something; working: wait; ready: start sandboxes; live: one is running. */
export type ReadinessStage = 'agent' | 'person' | 'working' | 'ready' | 'live'
export type RepoReadiness = {
  repo: string
  info: RepoInfo
  steps: ReadinessStep[]
  stage: ReadinessStage
  done: number
  total: number
  /** The first step to act on or wait for. */
  next: ReadinessStepId | null
}

export type TemplateState = TemplateOnRuntime & {
  repo: string
  log: string[]
  /** With several runtimes: the template state on each other enabled runtime. */
  runtimes?: TemplateOnRuntime[]
}
/** A repo's template on one runtime: the one its sandboxes start from there. */
export type TemplateOnRuntime = {
  runtime?: string
  status: 'building' | 'built' | 'failed' | 'interrupted' | '-' | string
  /** default, building, failed, interrupted, missing, stale or built; label and level say it for people (the server decides). */
  state: string
  label: string
  level: Level
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  stale?: boolean
}

export type RecipeFiles = { files: { path: string; text: string | null }[] }

export type GitHubRepository = { repo: string; description: string; private: boolean; branch: string }
export type RepoInspection = { repo: string; description: string; branch: string; private: boolean; github_app: boolean; registered: boolean; url: string }
export type SetupPr = { available: boolean; pull_request: null | { number: number; url: string; state: string; title?: string; draft?: boolean; merged?: boolean }; record?: RecipePr | null }

export type EnvKind = 'service' | 'sbx.yaml' | 'installation' | 'repo' | 'mine'
export type Audience = 'all' | 'previews' | 'sessions'
export type EnvRow = {
  name: string
  kind: EnvKind
  source: string
  secret?: boolean
  template?: string
  /** A value that is not secret. */
  value?: string
  note?: string
  audience?: string
  agent?: boolean
  mine?: boolean
  updated_by?: string
  updated_at?: string
  overridden_by?: string
  /** The attachment that gives this name: its key, and its package's title. */
  owned_by?: string
  attachment?: string
}

export type AttachmentState = 'ready' | 'needs_values' | 'needs_connection' | 'check_failed' | 'disabled' | 'error'
/** provision: a resource per sandbox; connection: a shared server or account saved once and provisioned on. */
export type PackageKind = 'provision' | 'connection'
export type Attachment = {
  key: string
  package: string
  kind: PackageKind
  title: string
  logo: string
  origin: 'sbx.yaml' | 'here' | 'both'
  inputs: Record<string, string>
  secret_inputs: Record<string, boolean>
  /** Requirement slot → the connection key it is bound to. */
  connections: Record<string, string>
  exports: string[]
  disabled: boolean
  state: AttachmentState
  problem: string | null
  project: boolean
}

export type EnvView = {
  repo: string
  admin: boolean
  variables: EnvRow[]
  missing: string[]
  attachments: Attachment[]
  /** Why saved values could not be read just now; the rows then lack them. */
  store_error: string
}

export type EnvChange = { running: number; paused: number; pending?: boolean; detail?: string }
export type SetVariable = { value: string | null; secret?: boolean | null; audience?: Audience; agent?: boolean; mine?: boolean; restart?: boolean }

/** label: the field's name for people; choices: the only values allowed, in order, each with its label. */
export type PackageInput = {
  required: boolean; secret: boolean; agent: boolean; help: string; default?: string
  label?: string; choices?: { value: string; label: string }[]
}
export type CatalogPackage = {
  name: string
  kind: PackageKind
  title: string
  description: string
  source: string
  inputs: Record<string, PackageInput>
  outputs: Record<string, { sensitive?: boolean }>
  env: Record<string, string>
  requires: Record<string, { kind: string; expose?: Record<string, string> }>
  runs: Record<string, string>
  checks: boolean
  docs: string
  /** data: URL of the SVG the package names as its logo, or "". */
  logo: string
  used_by: string[]
}
export type CatalogEntry = CatalogPackage & { readme: string; connections: SharedConnection[]; scripts: string[] }

/** A connection package's values saved once under a key (a Postgres server, a Neon project, an AWS role). */
export type SharedConnection = {
  key: string
  package: string
  title: string
  logo: string
  /** Non-secret values; secret ones read "(set)". */
  inputs: Record<string, string>
  check_status?: string | null
  check_detail?: string | null
  /** repo:attachment and sandbox resources that use it. */
  used_by: string[]
  added_by?: string
  added_at?: string
  known: boolean
  disabled?: boolean
  /** Set when this server runs it (a managed Postgres or ClickHouse). */
  managed?: { kind: string; status: string } | null
}

export type AttachBody = { package: string; key?: string; inputs: Record<string, string>; connections?: Record<string, string>; check?: boolean }

// Connections: platform connections and agent connectors share one shape.
export type ConnectionState = 'connected' | 'attention' | 'not_connected' | 'off'
export type ConnectionField = { name: string; label: string; secret: boolean; set: boolean; value: string | null; help: string }
export type Connection = {
  kind: string
  title: string
  /** platform: the server's own (domain, sign-in, GitHub); agent: a coding agent connector. */
  group: 'platform' | 'agent'
  state: ConnectionState
  summary: string
  /** Facts worth showing, formatted by the server. */
  display: { label: string; value: string }[]
  /** Machine facts (install_url, generated_password). */
  facts: Record<string, string | number | boolean | null>
  fields: ConnectionField[]
  actions: string[]
}
/** runtime: the kind of sandbox runtime it checks, null for the server's own checks. help_url: the owner's how-to page for what it checks, if any. */
export type Check = { name: string; ok: boolean; detail: string; fix: string; runtime?: string | null; help_url?: string }
export type CheckResult = { ok: boolean; checks: Check[] }

/** A key request an agent made (GET /api/secret-requests/ID): where each key goes, never a value. */
export type SecretRequestItem = {
  target: string
  label: string
  help_url: string
  what: string
  hint: string
  /** The target of the held key this item reuses (nothing to paste), or null. */
  reuse: string | null
  reuse_from: string | null
  done: boolean
  done_at: string | null
  done_by: string | null
  /** A runtime key's change on the server, once queued; null for any other key. */
  applied: RuntimeApply | null
}
/** How far the server is with runtime keys: queued and applied by the host, which then restarts sbx-control. */
export type RuntimeApply = { state: 'none' | 'queued' | 'applying' | 'restarting' | 'done' | 'failed' | 'unknown'; message: string }
export type SecretRequest = {
  id: string
  url: string
  state: 'pending' | 'completed' | 'cancelled' | 'expired'
  created_by: string
  created_at: string
  expires_at: string
  note: string
  finished_at: string | null
  finished_by: string | null
  remaining: number
  items: SecretRequestItem[]
  runtime_changes: RuntimeApply
}
export type SecretSubmitResult = { request: SecretRequest; ok: boolean; results: { target: string; ok: boolean; error: string }[] }

/** What saving or disconnecting an extension does to the templates (PUT/DELETE /api/connections/{kind}, extensions
 * only): how many rebuild on each runtime, whether they build now, and the server's sentence for it. */
export type Rebuilds = { runtimes: Record<string, number>; keys: Record<string, string[]>; building: boolean; message: string }
export type ConnectionChange = Connection & { rebuilds?: Rebuilds }
/** A coding agent the setup offers; chosen ones are in every template from the first build on. */
export type SetupAgent = { kind: string; title: string; chosen: boolean }
export type SetupState = {
  complete: boolean
  steps: { step: 'address' | 'sign_in' | 'runtime' | 'agents'; done: boolean; optional: boolean; summary: string }[]
  signed_in: boolean
  email: string | null
  /** Nobody signs in (a private network): no admin account to create. */
  open_access: boolean
  agents: SetupAgent[]
  can_complete: boolean
  host: { address: string }
  access: { mode: 'ip' | 'domain'; domain: string; url: string; certificate: string; certificate_email: string; certificate_ready: boolean; certificate_error: string; needs_ports: number[] }
  link: string
  admins: string[]
  version: string
}

export type Settings = {
  revision: number
  repos: string[]
  access: { admins: string[]; public_repos: boolean }
  offered: { public_repos: boolean; internal_cidrs: boolean }
  sandboxes: { paused_days: number; detected_previews: 'owner' | 'team' | 'off'; [key: string]: unknown }
  domain: { name: string; address: string; acme: string; acme_email: string; [key: string]: unknown }
  sign_in: { method: 'oidc' | 'password' | 'none'; allowed_emails: string[]; [key: string]: unknown }
  [section: string]: unknown
}

/** A managed shared server or internal host made reachable from provider sandboxes at address (GET /api/endpoints, admins).
 * server: the managed server's name ("" for an internal host). target: host:port, never shown. */
export type Endpoint = {
  name: string
  address: string
  kind: 'managed-server' | 'internal'
  server: string
  /** The managed server's kind ("postgres"), when the server sends it. */
  server_kind?: string
  target: string
  state: 'on' | 'applying' | 'failed'
  message: string
  used_by: string[]
  /** The repositories whose sandboxes reach it: an internal host's explicit grants (PUT replaces them);
   * for a managed server, the repos it is attached to (read-only here). */
  repos: string[]
}
/** A managed server not reachable yet. kind: the server's ("postgres", "clickhouse", "temporal"). address: the one it would get. */
export type EndpointCandidate = { server: string; kind: string; target: string; address?: string }
/** domain: "" until one is connected. needed: an enabled runtime can't reach this host's private network. */
export type Endpoints = { endpoints: Endpoint[]; candidates: EndpointCandidate[]; domain: string; needed: boolean }
export type AddEndpoint = { server: string } | { name: string; target: string; repos: string[] }
