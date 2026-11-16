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
}

export type Capacity = { known: boolean; memory_mb?: number; slots_total?: number; slots_free?: number; free_mb?: number; total_mb?: number }
export type SandboxList = { sandboxes: Sandbox[]; capacity: Capacity }
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

export type CreateSandbox = { name: string; branch?: string; repo?: string; public_keys: string }

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
}

export type ReadinessStepId = 'served' | 'github' | 'configured' | 'template' | 'services' | 'env' | 'sandbox'
export type ReadinessStepStatus = 'done' | 'todo' | 'working' | 'blocked'
export type ReadinessStep = {
  step: ReadinessStepId
  status: ReadinessStepStatus
  /** Who unblocks it, when it is not done. */
  who?: Actor
  /** Names of what is missing: env variables, attachment keys. */
  missing: string[]
  detail: string
  /** The github step: the App's install URL while it is not done. */
  action?: string
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

export type TemplateState = {
  repo: string
  status: 'building' | 'built' | 'failed' | '-' | string
  /** default, building, failed, missing, stale or built; label and level say it for people (the server decides). */
  state: string
  label: string
  level: Level
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  stale?: boolean
  log: string[]
}

export type RecipeFiles = { files: { path: string; text: string | null }[] }

export type GitHubRepository = { repo: string; description: string; private: boolean; branch: string }
export type RepoInspection = { repo: string; description: string; branch: string; private: boolean; github_app: boolean; registered: boolean; url: string }
export type SetupPr = { available: boolean; pull_request: null | { number: number; url: string; state: string; title?: string; draft?: boolean; merged?: boolean } }

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
export type Check = { name: string; ok: boolean; detail: string; fix: string }
export type CheckResult = { ok: boolean; checks: Check[] }

export type SetupState = {
  complete: boolean
  steps: { step: 'address' | 'sign_in'; done: boolean; optional: boolean; summary: string }[]
  signed_in: boolean
  email: string | null
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
