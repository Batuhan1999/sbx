import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setSetupToken } from '@/api/client'
import type { AvailableRuntime, RuntimeChange, SandboxRuntime } from '@/api/types'
import { Runtimes } from '@/pages/settings/Runtimes'
import SetupPage from '@/pages/setup/SetupPage'
import { ME, renderApp, stubApi } from './render'

const runtime = (kind: string, title: string, extra: Partial<SandboxRuntime> = {}): SandboxRuntime => ({
  kind, title, default: false, host: false, capabilities: { snapshots: false, private_networks: false, egress_rules: false }, limits: {}, status: { state: 'connected', summary: '' }, ...extra,
})
const EMBED = runtime('embed', 'E2B Embed', { default: true, host: true })
const CLOUD = runtime('e2b-cloud', 'E2B Cloud', { limit_hit: { field: 'timeout_seconds', asked: 7200, allowed: 3600, message: 'E2B refused a 2 h timeout: your plan allows 1 h', setting: 'E2B_CLOUD_MAX_TIMEOUT', at: '' } })

const LIMITS = [['E2B_CLOUD_MAX_TIMEOUT', 'Longest run (seconds)', '3600', 'timeout_seconds'], ['E2B_CLOUD_MAX_SANDBOXES', 'Sandboxes at once', '20', 'sandboxes']] as const
const available = (kind: string, title: string, extra: Partial<AvailableRuntime> = {}): AvailableRuntime => ({
  kind, title, host: false, enabled: false, default: false, can_run: true, why_not: '', managed_by: '', not_editable: '',
  settings: [], presets: [], values: {}, secrets_set: [], facts: {}, sandboxes: [], removable: '', limit_hit: null, ...extra,
})
const CLOUD_FORM: Partial<AvailableRuntime> = {
  settings: [
    { key: 'E2B_CLOUD_API_KEY', label: 'API key', secret: true, default: '', help: '', kind: 'text', optional: false, limit: '' },
    ...LIMITS.map(([key, label, d, limit]) => ({ key, label, secret: false, default: d, help: '', kind: 'int' as const, optional: false, limit })),
  ],
  presets: [{ name: 'hobby', title: 'Hobby', values: { E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_SANDBOXES: '10' }, source: 'https://e2b.dev/pricing' }],
}
const AV_EMBED = available('embed', 'E2B Embed', { host: true, enabled: true, default: true, managed_by: 'install.sh', not_editable: 'E2B Embed is set up by install.sh on this server', removable: 'E2B Embed is set up by install.sh on this server' })
const IDLE: RuntimeChange = { state: 'idle', operation: '', kind: '', message: '', at: '' }
const change = (state: RuntimeChange['state'], operation: RuntimeChange['operation'], message = ''): RuntimeChange => ({ state, operation, kind: 'e2b-cloud', message, at: '2026-11-17T12:00:00Z' })

/** Answers in turn, the last one again and again. */
function inTurn(...answers: { status: number; body: unknown }[]) {
  let i = 0
  return () => answers[Math.min(i++, answers.length - 1)]
}

describe('Settings → Sandbox runtimes', () => {
  it('adds a provider with a plan, follows the change through the restart and refreshes', async () => {
    let enabled = [EMBED]
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', CLOUD_FORM)],
      'GET /api/runtimes/change': inTurn({ status: 200, body: IDLE }, { status: 502, body: 'Bad Gateway' }, { status: 200, body: change('done', 'add', 'E2B Cloud (e2b-cloud) is enabled') }),
      'POST /api/runtimes': () => { enabled = [EMBED, runtime('e2b-cloud', 'E2B Cloud')]; return { status: 202, body: change('queued', 'add') } },
      'GET /api/runtimes': () => ({ status: 200, body: { default: 'embed', runtimes: enabled } }),
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add a runtime' }))
    const form = screen.getByRole('form', { name: 'E2B Cloud settings' })
    expect((within(form).getByLabelText('Sandboxes at once') as HTMLInputElement).value).toBe('10')   // Hobby's
    expect((within(form).getByLabelText('Sandboxes at once') as HTMLInputElement).disabled).toBe(true)
    await user.click(within(form).getByRole('radio', { name: 'Custom' }))
    await user.clear(within(form).getByLabelText('Sandboxes at once'))
    await user.type(within(form).getByLabelText('Sandboxes at once'), '50')
    await user.type(within(form).getByLabelText('API key'), 'test-only-key')
    await user.click(within(form).getByRole('button', { name: 'Add E2B Cloud' }))
    expect(await screen.findByText('Adding E2B Cloud…')).toBeTruthy()
    expect(calls.find(c => c.method === 'POST')!.body).toEqual({ kind: 'e2b-cloud', values: { E2B_CLOUD_API_KEY: 'test-only-key', E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_SANDBOXES: '50' } })
    expect(await screen.findByText('E2B Cloud (e2b-cloud) is enabled', undefined, { timeout: 8000 })).toBeTruthy()
    await waitFor(() => expect(screen.getAllByRole('heading', { name: 'E2B Cloud' }).length).toBe(1))
  }, 15000)

  it('changes settings with the key kept, makes a default, and says why one can’t be removed', async () => {
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, enabled: true, secrets_set: ['E2B_CLOUD_API_KEY'], values: { E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_SANDBOXES: '20' }, sandboxes: ['login'], removable: '1 sandbox run on E2B Cloud (login): delete them first' })],
      'GET /api/runtimes/change': IDLE,
      'PUT /api/runtimes/default': change('queued', 'default'),
      'PUT /api/runtimes/e2b-cloud': change('queued', 'change'),
      'GET /api/runtimes': { default: 'embed', runtimes: [EMBED, CLOUD] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    const cloud = (await screen.findByRole('heading', { name: 'E2B Cloud' })).closest('li')!
    expect(cloud.textContent).toContain('E2B refused a 2 h timeout: your plan allows 1 h')
    expect((await within(cloud).findByRole('button', { name: 'Remove' }) as HTMLButtonElement).disabled).toBe(true)
    expect(cloud.textContent).toContain("Can't remove: 1 sandbox run on E2B Cloud (login)")
    const embed = screen.getByRole('heading', { name: 'E2B Embed' }).closest('li')!
    expect(embed.textContent).toContain('Set up by install.sh on this server.')
    expect(within(embed).queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(within(embed).queryByRole('button', { name: 'Change settings' })).toBeNull()
    await user.click(within(cloud).getByRole('button', { name: 'Make default' }))
    await waitFor(() => expect(calls.find(c => c.url === '/api/runtimes/default')?.body).toEqual({ kind: 'e2b-cloud' }))
    expect(await screen.findByText('Making the default: E2B Cloud…')).toBeTruthy()
  })

  it('sends a changed setting without the key left empty', async () => {
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, enabled: true, secrets_set: ['E2B_CLOUD_API_KEY'], values: { E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_SANDBOXES: '20' } })],
      'GET /api/runtimes/change': IDLE,
      'PUT /api/runtimes/e2b-cloud': change('queued', 'change'),
      'GET /api/runtimes': { default: 'e2b-cloud', runtimes: [{ ...CLOUD, default: true, limit_hit: null }] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Change settings' }))
    const form = screen.getByRole('form', { name: 'E2B Cloud settings' })
    expect((within(form).getByLabelText('API key') as HTMLInputElement).placeholder).toBe('Set · type to replace')
    expect(within(form).getByRole('radio', { name: 'Custom' })).toHaveProperty('checked', true)   // 20 at once is no plan's
    await user.clear(within(form).getByLabelText('Longest run (seconds)'))
    await user.type(within(form).getByLabelText('Longest run (seconds)'), '86400')
    await user.click(within(form).getByRole('button', { name: 'Save settings' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
    expect(calls.find(c => c.method === 'PUT')!.body).toEqual({ values: { E2B_CLOUD_MAX_TIMEOUT: '86400', E2B_CLOUD_MAX_SANDBOXES: '20' } })
  })

  it('shows the server’s refusal, and what to do on a local secret store instead of the form', async () => {
    stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, not_editable: 'set up the secret store first, or on the server: sbx-admin runtime add e2b-cloud' })],
      'GET /api/runtimes/change': change('failed', 'add', 'E2B Cloud did not take the settings: E2B Cloud API: HTTP 401'),
      'GET /api/runtimes': { default: 'embed', runtimes: [EMBED] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    expect((await screen.findByRole('alert')).textContent).toContain('Adding E2B Cloud failed: E2B Cloud did not take the settings: E2B Cloud API: HTTP 401')
    await user.click(await screen.findByRole('button', { name: 'Add a runtime' }))
    expect(screen.getByText(/sbx-admin runtime add e2b-cloud/)).toBeTruthy()
    expect(screen.queryByRole('form', { name: 'E2B Cloud settings' })).toBeNull()
  })

  it('shows nothing to change for someone who is not an admin', async () => {
    const calls = stubApi({ 'GET /api/me': { ...ME, admin: false }, 'GET /api/runtimes': { default: 'embed', runtimes: [EMBED] } })
    renderApp(<Runtimes />)
    expect(await screen.findByRole('heading', { name: 'E2B Embed' })).toBeTruthy()
    await waitFor(() => expect(calls.some(c => c.url === '/api/me')).toBe(true))
    expect(screen.queryByRole('button')).toBeNull()
    expect(calls.some(c => c.url.startsWith('/api/runtimes/'))).toBe(false)
  })
})

const SETUP = (runtimeDone: boolean) => ({
  complete: false,
  steps: [{ step: 'address', done: true, optional: false, summary: '' }, { step: 'sign_in', done: false, optional: false, summary: '' },
    { step: 'runtime', done: runtimeDone, optional: false, summary: '' }],
  signed_in: false, email: null, can_complete: false, host: { address: '203.0.113.7' },
  access: { mode: 'domain', domain: 'sbx.example.com', url: 'https://sbx.example.com', certificate: 'delegated', certificate_email: '', certificate_ready: true, certificate_error: '', needs_ports: [] },
  link: 'http://localhost:3000/app/setup', admins: [], version: 'dev',
})

describe('first-run setup without a runtime', () => {
  it('offers the runtimes this server can run, adds one and continues once it is applied', async () => {
    setSetupToken('one-time-token')
    let added = false
    const polls = inTurn({ status: 200, body: IDLE }, { status: 200, body: change('applying', 'add') }, { status: 200, body: change('done', 'add', 'E2B Cloud (e2b-cloud) is enabled') })
    const calls = stubApi({
      'GET /api/setup/checks': { ok: true, checks: [] },
      'GET /api/setup': () => ({ status: 200, body: SETUP(added) }),
      'GET /api/runtimes/available': [available('embed', 'E2B Embed', { host: true, can_run: false, why_not: 'needs /dev/kvm: bare metal, or a VM with nested virtualization enabled' }), available('e2b-cloud', 'E2B Cloud', CLOUD_FORM)],
      'GET /api/runtimes/change': () => {   // applying until the host has it, then done (runtime.env names it)
        const answer = polls()
        added = (answer.body as RuntimeChange).state === 'done'
        return answer
      },
      'POST /api/runtimes': () => ({ status: 202, body: change('queued', 'add') }),
      'GET /api/runtimes': () => ({ status: 200, body: { default: added ? 'e2b-cloud' : '', runtimes: added ? [runtime('e2b-cloud', 'E2B Cloud')] : [] } }),
    })
    renderApp(<SetupPage />, '/app/setup')
    const user = userEvent.setup()
    expect(await screen.findByRole('heading', { name: 'Where will sandboxes run?' })).toBeTruthy()
    const embed = await screen.findByRole('radio', { name: /E2B Embed/ }) as HTMLInputElement
    expect(embed.disabled).toBe(true)
    expect(screen.getByText("Can't run here: needs /dev/kvm: bare metal, or a VM with nested virtualization enabled")).toBeTruthy()
    expect((screen.getByRole('radio', { name: /E2B Cloud/ }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('button', { name: /Continue/ }) as HTMLButtonElement).disabled).toBe(true)
    await user.type(screen.getByLabelText('API key'), 'test-only-key')
    await user.click(screen.getByRole('button', { name: 'Add E2B Cloud' }))
    expect(await screen.findByText('Adding E2B Cloud…')).toBeTruthy()
    // Applied: on to the admin step, the new runtime listed with the server's checks.
    expect(await screen.findByRole('heading', { name: 'Who runs this server?' }, { timeout: 10000 })).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('list', { name: 'Sandbox runtimes' }).textContent).toContain('E2B Cloud'))
    const post = calls.find(c => c.method === 'POST' && c.url === '/api/runtimes')!
    expect(post.body).toMatchObject({ kind: 'e2b-cloud', values: { E2B_CLOUD_API_KEY: 'test-only-key' } })
  }, 20000)
})
