// One mapping from the server's lifecycle record to what the UI shows. Every
// status badge, filter and polling decision reads sandboxStatus().
import type { Lifecycle, LifecyclePhase, Sandbox } from '@/api/types'

export type Tone = 'ok' | 'warn' | 'bad' | 'progress' | 'idle'
export type StatusKey =
  | 'ready' | 'unverified' | 'workspace' | 'creating' | 'starting' | 'restarting' | 'preparing' | 'updating'
  | 'resuming' | 'pausing' | 'deleting' | 'paused' | 'failed' | 'missing' | 'deleted' | 'unknown'
export type StatusView = { key: StatusKey; label: string; tone: Tone; busy: boolean; detail?: string }

export const PHASE_LABEL: Record<LifecyclePhase, string> = {
  queued: 'Queued',
  allocating: 'Allocating the VM',
  provisioning_services: 'Provisioning services',
  preparing_workspace: 'Preparing the workspace',
  workspace_ready: 'Workspace ready',
  services_ready: 'Services ready',
  setting_up: 'Running setup',
  starting_app: 'Starting the app',
  restoring_runtime: 'Restoring from snapshot',
  pausing: 'Pausing',
  deleting: 'Deleting',
  app_ready: 'App healthy',
  app_unverified: 'App started, not verified',
  failed: 'Failed',
  interrupted: 'Interrupted',
  deleted: 'Deleted',
  unknown: 'Unknown',
}

const OPERATION: Record<string, [StatusKey, string]> = {
  create: ['creating', 'Creating'],
  resume: ['resuming', 'Resuming'],
  pause: ['pausing', 'Pausing'],
  delete: ['deleting', 'Deleting'],
  restart: ['restarting', 'Restarting'],
  prepare: ['preparing', 'Preparing'],
  env_update: ['updating', 'Updating env'],
}

type StatusInput = Pick<Sandbox, 'status' | 'vm' | 'error'> & { lifecycle?: Lifecycle | null }

export function sandboxStatus(s: StatusInput): StatusView {
  const lc = s.lifecycle ?? null
  if (s.status === 'deleted' || lc?.phase === 'deleted') return { key: 'deleted', label: 'Deleted', tone: 'idle', busy: false }
  if (lc?.active) {
    const [key, label] = OPERATION[lc.operation] ?? ['starting', 'Working']
    return { key, label, tone: 'progress', busy: true, detail: PHASE_LABEL[lc.phase] ?? lc.phase }
  }
  if (s.status === 'creating') return { key: 'creating', label: 'Creating', tone: 'progress', busy: true }
  if (s.status === 'starting') return { key: 'starting', label: 'Starting', tone: 'progress', busy: true }
  if (s.status === 'failed') {
    return { key: 'failed', label: lc?.phase === 'interrupted' ? 'Interrupted' : 'Failed', tone: 'bad', busy: false, detail: s.error ?? lc?.error ?? undefined }
  }
  if (s.status === 'paused' || s.vm === 'paused') return { key: 'paused', label: 'Paused', tone: 'idle', busy: false, detail: 'Opening a preview resumes it' }
  if (s.vm === 'missing') return { key: 'missing', label: 'VM missing', tone: 'bad', busy: false, detail: 'The runtime has no VM for this sandbox' }
  if (s.status === 'up') return { key: 'ready', label: 'Ready', tone: 'ok', busy: false, detail: 'App healthy' }
  if (s.status === 'workspace_ready') {
    if (lc?.application === 'unverified') return { key: 'unverified', label: 'Running', tone: 'warn', busy: false, detail: 'App started; no passing health check' }
    return { key: 'workspace', label: 'Workspace ready', tone: 'idle', busy: false, detail: 'The app is not started' }
  }
  return { key: 'unknown', label: 'Unknown', tone: 'idle', busy: false }
}

export const isBusy = (s: StatusInput) => sandboxStatus(s).busy

export type StepState = 'done' | 'current' | 'pending' | 'failed' | 'skipped'
export type LifecycleStep = { key: string; label: string; state: StepState }

// The order an operation passes the checkpoints in (lifecycle.EDGES).
// VM allocation counts toward the workspace: nobody acts on it.
const STEPS: { key: string; label: string; phases: LifecyclePhase[]; done: (r: Lifecycle) => boolean }[] = [
  { key: 'services', label: 'Services', phases: ['provisioning_services'], done: r => r.services_ready },
  { key: 'workspace', label: 'Workspace', phases: ['allocating', 'restoring_runtime', 'preparing_workspace'], done: r => r.workspace_ready },
  { key: 'setup', label: 'Setup', phases: ['setting_up'], done: r => ['starting_app', 'healthy', 'unverified'].includes(r.application) },
  { key: 'start', label: 'App start', phases: ['starting_app'], done: r => ['healthy', 'unverified'].includes(r.application) },
  { key: 'health', label: 'Healthy', phases: [], done: r => r.application === 'healthy' },
]

export function lifecycleSteps(record: Lifecycle | null | undefined): LifecycleStep[] {
  if (!record) return STEPS.map(s => ({ key: s.key, label: s.label, state: 'pending' }))
  const failed = record.phase === 'failed' || record.phase === 'interrupted'
  const out: LifecycleStep[] = STEPS.map(s => ({
    key: s.key, label: s.label,
    state: s.done(record) ? 'done' : record.active && s.phases.includes(record.phase) ? 'current' : 'pending',
  }))
  if (failed || record.application === 'failed') {
    const first = out.find(s => s.state !== 'done')
    if (first) first.state = 'failed'
  }
  if (!failed && !record.active && record.application === 'unverified') {
    out[out.length - 1].state = 'skipped'
  }
  return out
}
