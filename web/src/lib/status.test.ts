import { describe, expect, it } from 'vitest'
import type { Lifecycle, Sandbox } from '@/api/types'
import { lifecycleSteps, sandboxStatus } from './status'

const record = (over: Partial<Lifecycle> = {}): Lifecycle => ({
  revision: 1, phase: 'app_ready', operation: 'create', operation_id: 'o', active: false, runtime: 'running',
  workspace_ready: true, services_ready: true, application: 'healthy', sandbox_id: 'vm', error: null, ...over,
})
const sb = (over: Partial<Sandbox> = {}) => ({ status: 'up' as Sandbox['status'], vm: 'running', error: null, lifecycle: record(), ...over })

describe('sandboxStatus', () => {
  it('reads a healthy app as ready', () => {
    expect(sandboxStatus(sb())).toMatchObject({ key: 'ready', tone: 'ok', busy: false })
  })

  it('names the running operation and its phase while busy', () => {
    const v = sandboxStatus(sb({ status: 'creating', lifecycle: record({ active: true, operation: 'create', phase: 'provisioning_services', services_ready: false }) }))
    expect(v).toMatchObject({ key: 'creating', label: 'Creating', tone: 'progress', busy: true, detail: 'Provisioning services' })
    expect(sandboxStatus(sb({ lifecycle: record({ active: true, operation: 'resume', phase: 'restoring_runtime' }) })).label).toBe('Resuming')
    expect(sandboxStatus(sb({ lifecycle: record({ active: true, operation: 'pause', phase: 'pausing' }) })).key).toBe('pausing')
    expect(sandboxStatus(sb({ lifecycle: record({ active: true, operation: 'env_update', phase: 'preparing_workspace' }) })).label).toBe('Updating env')
  })

  it('treats a sandbox still being created before its VM exists as busy', () => {
    expect(sandboxStatus({ status: 'creating', vm: '', error: null })).toMatchObject({ key: 'creating', busy: true })
  })

  it('shows failures with the error, and interrupted operations by name', () => {
    expect(sandboxStatus(sb({ status: 'failed', error: 'create failed during setting_up (CalledProcessError)', lifecycle: record({ phase: 'failed', application: 'failed' }) })))
      .toMatchObject({ key: 'failed', tone: 'bad', detail: 'create failed during setting_up (CalledProcessError)' })
    expect(sandboxStatus(sb({ status: 'failed', lifecycle: record({ phase: 'interrupted' }) })).label).toBe('Interrupted')
  })

  it('prefers the VM being paused over the last app state', () => {
    expect(sandboxStatus(sb({ vm: 'paused' }))).toMatchObject({ key: 'paused', tone: 'idle' })
  })

  it('separates an unverified app from a workspace without one', () => {
    expect(sandboxStatus(sb({ status: 'workspace_ready', lifecycle: record({ application: 'unverified', phase: 'app_unverified' }) }))).toMatchObject({ key: 'unverified', tone: 'warn' })
    expect(sandboxStatus(sb({ status: 'workspace_ready', lifecycle: record({ application: 'not_started', phase: 'workspace_ready' }) }))).toMatchObject({ key: 'workspace' })
  })

  it('maps deleted and unknown', () => {
    expect(sandboxStatus(sb({ status: 'deleted' })).key).toBe('deleted')
    expect(sandboxStatus({ status: 'unknown', vm: 'running', error: null }).key).toBe('unknown')
    expect(sandboxStatus(sb({ vm: 'missing' })).key).toBe('missing')
  })
})

describe('lifecycleSteps', () => {
  const states = (r: Lifecycle | null) => lifecycleSteps(r).map(s => s.state)

  it('marks checkpoints done and the active phase current', () => {
    expect(states(record({ active: true, phase: 'preparing_workspace', workspace_ready: false, application: 'not_started' })))
      .toEqual(['done', 'current', 'pending', 'pending', 'pending'])
  })

  it('marks the first unfinished checkpoint failed', () => {
    expect(states(record({ phase: 'failed', application: 'failed' }))).toEqual(['done', 'done', 'failed', 'pending', 'pending'])
  })

  it('skips the health step when the app started without a passing check', () => {
    expect(states(record({ phase: 'app_unverified', application: 'unverified' }))).toEqual(['done', 'done', 'done', 'done', 'skipped'])
  })

  it('counts VM allocation toward the workspace step', () => {
    expect(states(record({ active: true, phase: 'allocating', workspace_ready: false, services_ready: true, application: 'not_started' })))
      .toEqual(['done', 'current', 'pending', 'pending', 'pending'])
    expect(lifecycleSteps(null).map(s => s.key)).not.toContain('vm')
  })

  it('is all pending without a record', () => {
    expect(states(null).every(s => s === 'pending')).toBe(true)
  })
})
