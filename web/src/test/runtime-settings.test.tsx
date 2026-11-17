import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setSetupToken } from '@/api/client'
import type { AvailableRuntime, RuntimeChange, SandboxRuntime } from '@/api/types'
import { Runtimes, runtimeLine } from '@/pages/settings/Runtimes'
import SetupPage from '@/pages/setup/SetupPage'
import { ME, renderApp, stubApi } from './render'

const runtime = (kind: string, title: string, extra: Partial<SandboxRuntime> = {}): SandboxRuntime => ({
  kind, title, default: false, host: false, capabilities: { snapshots: false, private_networks: false, egress_rules: false }, limits: {}, status: { state: 'connected', summary: '' }, ...extra,
})
const EMBED = runtime('embed', 'E2B Embed', { default: true, host: true })
const CLOUD = runtime('e2b-cloud', 'E2B Cloud', { limit_hit: { field: 'timeout_seconds', asked: 7200, allowed: 3600, message: 'E2B refused a 2 h timeout: your plan allows 1 h', setting: 'E2B_CLOUD_MAX_TIMEOUT', at: '' } })

// As E2B Cloud declares them (runtime/e2b_cloud: SETTINGS, PRESETS).
const LIMITS = [
  ['E2B_CLOUD_MAX_TIMEOUT', 'Longest run', '3600', 'timeout_seconds', 'seconds', 300, 86400, 300],
  ['E2B_CLOUD_MAX_CPU', 'vCPUs', '8', 'cpu', '', 1, 32, 1],
  ['E2B_CLOUD_MAX_MEMORY_MB', 'Memory', '8192', 'memory_mb', 'MiB', 1024, 65536, 1024],
  ['E2B_CLOUD_MAX_DISK_MB', 'Disk', '10240', 'disk_mb', 'MiB', 1024, 20480, 1024],
  ['E2B_CLOUD_MAX_SANDBOXES', 'Sandboxes at once', '20', 'sandboxes', '', 1, 100, 1],
] as const
const HOBBY = { E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_CPU: '8', E2B_CLOUD_MAX_MEMORY_MB: '8192', E2B_CLOUD_MAX_DISK_MB: '10240', E2B_CLOUD_MAX_SANDBOXES: '10' }
const PRO = { E2B_CLOUD_MAX_TIMEOUT: '86400', E2B_CLOUD_MAX_CPU: '8', E2B_CLOUD_MAX_MEMORY_MB: '8192', E2B_CLOUD_MAX_DISK_MB: '20480', E2B_CLOUD_MAX_SANDBOXES: '100' }
const available = (kind: string, title: string, extra: Partial<AvailableRuntime> = {}): AvailableRuntime => ({
  kind, title, host: false, enabled: false, default: false, can_run: true, why_not: '', managed_by: '', edit_blocker: null,
  settings: [], presets: [], values: {}, secrets_set: [], facts: {}, sandboxes: [], remove_blocker: null, limit_hit: null, ...extra,
})
const CLOUD_FORM: Partial<AvailableRuntime> = {
  settings: [
    { key: 'E2B_CLOUD_API_KEY', label: 'API key', secret: true, default: '', help: '', kind: 'text', optional: false, limit: '' },
    { key: 'E2B_CLOUD_DOMAIN', label: 'Domain', secret: false, default: 'e2b.app', help: '', kind: 'hostname', optional: true, limit: '', rekey: true },
    ...LIMITS.map(([key, label, d, limit, unit, min, max, step]) => ({ key, label, secret: false, default: d, help: '', kind: 'int' as const, optional: false, limit, unit, min, max, step })),
  ],
  presets: [{ name: 'hobby', title: 'Hobby', values: HOBBY, source: 'https://e2b.dev/pricing' }, { name: 'pro', title: 'Pro', values: PRO, source: 'https://e2b.dev/pricing', adjustable: ['E2B_CLOUD_MAX_CPU', 'E2B_CLOUD_MAX_MEMORY_MB'], note: 'Pro allows more than 8 vCPUs and 8 GB if E2B raised it for your team; E2B says so when it doesn’t.' }],
}
const AV_EMBED = available('embed', 'E2B Embed', { host: true, enabled: true, default: true, managed_by: 'install.sh', edit_blocker: { code: 'host', message: 'E2B Embed is set up by install.sh on this server', sandboxes: [] }, remove_blocker: { code: 'host', message: 'E2B Embed is set up by install.sh on this server; remove it there: sbx-admin runtime remove embed', sandboxes: [] } })
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
    const done = change('done', 'add', 'E2B Cloud (e2b-cloud) is enabled')
    const polls = inTurn({ status: 200, body: IDLE }, { status: 200, body: { ...done, restarting: true } }, { status: 502, body: 'Bad Gateway' }, { status: 200, body: done })
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', CLOUD_FORM)],
      // Done, first from the process before the restart, then nothing (restarting), then the new process.
      'GET /api/runtimes/change': () => {
        const answer = polls()
        if ((answer.body as RuntimeChange).state === 'done' && !(answer.body as RuntimeChange).restarting) enabled = [EMBED, runtime('e2b-cloud', 'E2B Cloud')]
        return answer
      },
      'POST /api/runtimes': () => ({ status: 202, body: change('queued', 'add') }),
      'GET /api/runtimes': () => ({ status: 200, body: { default: 'embed', runtimes: enabled } }),
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add runtime' }))
    const form = screen.getByRole('form', { name: 'E2B Cloud settings' })
    // The plans as cards, Hobby chosen: their limits in words, nothing to edit.
    const hobby = within(form).getByRole('radio', { name: /Hobby/ }) as HTMLInputElement
    expect(hobby.checked).toBe(true)
    expect(hobby.closest('label')!.textContent).toContain('1 h · 10 at once8 vCPU · 8 GB · 10 GB disk')   // two lines
    expect(within(form).getByRole('radio', { name: /Pro/ }).closest('label')!.textContent).toContain('24 h · 100 at once8+ vCPU · 8+ GB · 20 GB disk')
    expect(within(form).queryByRole('slider')).toBeNull()
    expect(within(form).getByRole('link', { name: /Check your plan/ }).getAttribute('href')).toBe('https://e2b.dev/pricing')
    expect(within(form).queryByText(/2026/)).toBeNull()
    // Custom: a slider per limit, in people's units, and a precise entry.
    await user.click(within(form).getByRole('radio', { name: /Custom/ }))
    expect(within(form).getAllByRole('slider')).toHaveLength(5)
    const run = within(form).getByRole('slider', { name: 'Longest run' }) as HTMLInputElement
    expect(run.getAttribute('aria-valuetext')).toBe('1 h')
    expect([run.min, run.max, run.step]).toEqual(['0.5', '24', '0.5'])   // in hours, as shown
    await user.clear(within(form).getByLabelText('Longest run (exact)'))
    await user.type(within(form).getByLabelText('Longest run (exact)'), '1.5{Enter}')
    expect(run.getAttribute('aria-valuetext')).toBe('1.5 h')
    await user.clear(within(form).getByLabelText('Memory (exact)'))
    await user.type(within(form).getByLabelText('Memory (exact)'), '4{Enter}')
    expect(within(form).getByRole('slider', { name: 'Memory' }).getAttribute('aria-valuetext')).toBe('4 GB')
    await user.clear(within(form).getByLabelText('Sandboxes at once (exact)'))
    await user.type(within(form).getByLabelText('Sandboxes at once (exact)'), '500{Enter}')   // held to the plan's range
    expect(within(form).getByRole('slider', { name: 'Sandboxes at once' }).getAttribute('aria-valuetext')).toBe('100')
    within(form).getByRole('slider', { name: 'Sandboxes at once' }).focus()
    await user.keyboard('{Shift>}{ArrowLeft}{/Shift}')                  // Shift+arrow: ten steps
    expect(within(form).getByRole('slider', { name: 'Sandboxes at once' }).getAttribute('aria-valuetext')).toBe('90')
    await user.keyboard('{Shift>}{ArrowRight}{/Shift}')
    expect(within(form).getAllByRole('slider').map(s => s.getAttribute('aria-label') ?? (s as HTMLInputElement).labels?.[0]?.textContent))
      .toEqual(['Longest run', 'Sandboxes at once', 'vCPUs', 'Memory', 'Disk'])   // run and how many first, then the size
    await user.click(within(form).getByRole('button', { name: 'Add E2B Cloud' }))
    expect(within(form).getByText('Enter the API key.')).toBeTruthy()
    await user.type(within(form).getByLabelText('API key'), 'test-only-key')
    await user.click(within(form).getByRole('button', { name: 'Add E2B Cloud' }))
    expect(await screen.findByText('Applying…')).toBeTruthy()
    expect(calls.find(c => c.method === 'POST')!.body).toEqual({ kind: 'e2b-cloud', plan: 'custom', values: {
      E2B_CLOUD_API_KEY: 'test-only-key', E2B_CLOUD_MAX_TIMEOUT: '5400', E2B_CLOUD_MAX_CPU: '8', E2B_CLOUD_MAX_MEMORY_MB: '4096',
      E2B_CLOUD_MAX_DISK_MB: '10240', E2B_CLOUD_MAX_SANDBOXES: '100' } })
    await waitFor(() => expect(calls.filter(c => c.url === '/api/runtimes/change').length).toBeGreaterThanOrEqual(2), { timeout: 4000 })
    expect(screen.getByText('Applying…')).toBeTruthy()            // done, but the old process answered: still waiting
    expect(await screen.findByText('Added', undefined, { timeout: 10000 })).toBeTruthy()
    await waitFor(() => expect(screen.getAllByRole('heading', { name: 'E2B Cloud' }).length).toBe(1))
  }, 25000)

  it('changes settings with the key kept, makes a default, and says why one can’t be removed', async () => {
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, enabled: true, secrets_set: ['E2B_CLOUD_API_KEY'], values: { E2B_CLOUD_MAX_TIMEOUT: '3600', E2B_CLOUD_MAX_SANDBOXES: '20' }, sandboxes: ['login'], remove_blocker: { code: 'has_sandboxes', message: '1 sandbox run on E2B Cloud (login): delete them first (sbx down NAME, or the web app)', sandboxes: ['login'] } })],
      'GET /api/runtimes/change': IDLE,
      'PUT /api/runtimes/default': change('queued', 'default'),
      'PUT /api/runtimes/e2b-cloud': change('queued', 'change'),
      'GET /api/runtimes': { default: 'embed', runtimes: [EMBED, CLOUD] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    const cloud = (await screen.findByRole('heading', { name: 'E2B Cloud' })).closest('li')!
    expect(cloud.textContent).toContain('E2B refused a 2 h timeout: your plan allows 1 h.')
    expect((await within(cloud).findByRole('button', { name: 'Remove' }) as HTMLButtonElement).disabled).toBe(true)
    expect(cloud.querySelector('[title]')!.getAttribute('title')).toBe('Delete its sandboxes first (login)')
    const embed = screen.getByRole('heading', { name: 'E2B Embed' }).closest('li')!
    expect(embed.textContent).toContain('Managed by install.sh on this server.')
    expect(within(embed).queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(within(embed).queryByRole('button', { name: 'Edit' })).toBeNull()
    await user.click(within(cloud).getByRole('button', { name: 'Make default' }))
    await waitFor(() => expect(calls.find(c => c.url === '/api/runtimes/default')?.body).toEqual({ kind: 'e2b-cloud' }))
    expect(await screen.findByText('Applying…')).toBeTruthy()
  })

  it('preselects the plan the stored limits are, keeps the key, and moves to another plan', async () => {
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, enabled: true, secrets_set: ['E2B_CLOUD_API_KEY'], values: { ...HOBBY }, plan: { name: 'hobby', title: 'Hobby' } })],
      'GET /api/runtimes/change': IDLE,
      'PUT /api/runtimes/e2b-cloud': change('queued', 'change'),
      'GET /api/runtimes': { default: 'e2b-cloud', runtimes: [{ ...CLOUD, default: true, limit_hit: null, plan: { name: 'hobby', title: 'Hobby' },
        limits: { timeout_seconds: 3600, cpu: 8, memory_mb: 8192, disk_mb: 10240, sandboxes: 10 } }] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    const row = (await screen.findByRole('heading', { name: 'E2B Cloud' })).closest('li')!
    expect(row.textContent).toContain('Hobby · up to 10 at once')
    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    const form = screen.getByRole('form', { name: 'E2B Cloud settings' })
    expect((within(form).getByLabelText('API key') as HTMLInputElement).placeholder).toBe('Saved · type to replace')
    expect(within(form).getByRole('radio', { name: /Hobby/ })).toHaveProperty('checked', true)
    expect(within(form).queryByRole('slider')).toBeNull()                 // Hobby fixes every limit
    await user.click(within(form).getByRole('radio', { name: /Pro/ }))
    // Pro fixes its run, disk and sandboxes; its vCPUs and memory are "8+": sliders from 8 and 8 GB.
    expect(within(form).getAllByRole('slider').map(s => s.getAttribute('aria-label') ?? (s as HTMLInputElement).labels?.[0]?.textContent)).toEqual(['vCPUs', 'Memory'])
    expect(within(form).getByText(/Pro allows more than 8 vCPUs and 8 GB/)).toBeTruthy()
    expect(within(form).getByRole('slider', { name: 'vCPUs' }).getAttribute('aria-valuetext')).toBe('8')
    await user.clear(within(form).getByLabelText('vCPUs (exact)'))
    await user.type(within(form).getByLabelText('vCPUs (exact)'), '16{Enter}')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
    expect(calls.find(c => c.method === 'PUT')!.body).toEqual({ plan: 'pro', values: { E2B_CLOUD_MAX_CPU: '16' } })   // the server sets Pro's limits; no key: kept; the domain is as it was
  })

  it('shows Custom for limits no plan has, and asks for the key again when the domain moves', async () => {
    const calls = stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, enabled: true, secrets_set: ['E2B_CLOUD_API_KEY'], values: { ...HOBBY, E2B_CLOUD_MAX_SANDBOXES: '200' }, plan: { name: 'custom', title: 'Custom limits' } })],
      'GET /api/runtimes/change': IDLE,
      'PUT /api/runtimes/e2b-cloud': change('queued', 'change'),
      'GET /api/runtimes': { default: 'e2b-cloud', runtimes: [{ ...CLOUD, default: true, limit_hit: null, plan: { name: 'custom', title: 'Custom limits' }, limits: { sandboxes: 20 } }] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    expect((await screen.findByRole('heading', { name: 'E2B Cloud' })).closest('li')!.textContent).toContain('Custom limits · up to 20 at once')
    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    const form = screen.getByRole('form', { name: 'E2B Cloud settings' })
    expect(within(form).getByRole('radio', { name: /Custom/ })).toHaveProperty('checked', true)
    expect(within(form).getByRole('slider', { name: 'Sandboxes at once' }).getAttribute('aria-valuetext')).toBe('100')   // 200 by hand in runtime.env: the slider's top
    const advanced = within(form).getByText('Advanced').closest('details')!
    expect(advanced.open).toBe(false)                                   // the domain is out of the way
    await user.click(within(form).getByText('Advanced'))
    expect(within(advanced).getByText(/Changing it needs the API key again./)).toBeTruthy()
    await user.type(within(form).getByLabelText(/Domain/), 'e2b.example.com')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await within(form).findByText('Enter the API key again to change the domain.')).toBeTruthy()
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    await user.type(within(form).getByLabelText('API key'), 'test-only-key')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
    expect(calls.find(c => c.method === 'PUT')!.body).toEqual({ values: { E2B_CLOUD_API_KEY: 'test-only-key', E2B_CLOUD_DOMAIN: 'e2b.example.com' } })   // the untouched 200 is never lowered
  })

  it('shows the server’s refusal, and what to do on a local secret store instead of the form', async () => {
    stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED, available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM, edit_blocker: { code: 'local_store', message: 'set up the secret store first, or on the server: sbx-admin runtime add e2b-cloud', sandboxes: [] } })],
      'GET /api/runtimes/change': { ...change('failed', 'add', 'E2B Cloud did not take the settings: E2B Cloud API: HTTP 401'), at: new Date().toISOString() },
      'GET /api/runtimes': { default: 'embed', runtimes: [EMBED] },
    })
    renderApp(<Runtimes />)
    const user = userEvent.setup()
    expect((await screen.findByRole('alert')).textContent).toContain("Couldn't add: E2B Cloud did not take the settings: E2B Cloud API: HTTP 401")
    await user.click(await screen.findByRole('button', { name: 'Add runtime' }))
    expect(screen.getByText('Add it on the server instead: sbx-admin runtime add e2b-cloud')).toBeTruthy()
    expect(screen.queryByRole('form', { name: 'E2B Cloud settings' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/Couldn't add/)).toBeNull()
  })

  it('does not show someone else’s old failure', async () => {
    stubApi({
      'GET /api/me': ME,
      'GET /api/runtimes/available': [AV_EMBED],
      'GET /api/runtimes/change': { ...change('failed', 'add', 'HTTP 401'), at: new Date(Date.now() - 60 * 60_000).toISOString() },
      'GET /api/runtimes': { default: 'embed', runtimes: [EMBED] },
    })
    renderApp(<Runtimes />)
    expect(await screen.findByRole('heading', { name: 'E2B Embed' })).toBeTruthy()
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByText(/Couldn't/)).toBeNull()
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
    expect(screen.getByText('Needs /dev/kvm')).toBeTruthy()
    expect((screen.getByRole('radio', { name: /E2B Cloud/ }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('button', { name: /Continue/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('radio', { name: /Hobby/ }) as HTMLInputElement).checked).toBe(true)   // the same form as in Settings
    await user.type(screen.getByLabelText('API key'), 'test-only-key')
    await user.click(screen.getByRole('button', { name: 'Add E2B Cloud' }))
    expect(await screen.findByText('Applying…')).toBeTruthy()
    // Applied: on to the admin step, the new runtime listed with the server's checks.
    expect(await screen.findByRole('heading', { name: 'Who runs this server?' }, { timeout: 10000 })).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('list', { name: 'Sandbox runtimes' }).textContent).toContain('E2B Cloud'))
    const post = calls.find(c => c.method === 'POST' && c.url === '/api/runtimes')!
    expect(post.body).toMatchObject({ kind: 'e2b-cloud', values: { E2B_CLOUD_API_KEY: 'test-only-key' } })
  }, 20000)

  it('keeps what was typed when applying the runtime fails', async () => {
    setSetupToken('one-time-token')
    const polls = inTurn({ status: 200, body: IDLE }, { status: 200, body: change('applying', 'add') }, { status: 200, body: change('failed', 'add', 'E2B Cloud refused the key') })
    stubApi({
      'GET /api/setup/checks': { ok: true, checks: [] },
      'GET /api/setup': SETUP(false),
      'GET /api/runtimes/available': [available('e2b-cloud', 'E2B Cloud', CLOUD_FORM)],
      'GET /api/runtimes/change': () => polls(),
      'POST /api/runtimes': () => ({ status: 202, body: change('queued', 'add') }),
      'GET /api/runtimes': { default: '', runtimes: [] },
    })
    renderApp(<SetupPage />, '/app/setup')
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('API key'), 'test-only-key')
    await user.click(screen.getByRole('button', { name: 'Add E2B Cloud' }))
    expect(await screen.findByText(/E2B Cloud refused the key/, undefined, { timeout: 10000 })).toBeTruthy()
    await waitFor(() => expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe('test-only-key'))
  }, 20000)

  it('says up front that a provider needs a domain, and goes back to the address step', async () => {
    setSetupToken('one-time-token')
    stubApi({
      'GET /api/setup/checks': { ok: true, checks: [] },
      'GET /api/setup': { ...SETUP(false), access: { ...SETUP(false).access, mode: 'ip', domain: '' } },
      'GET /api/runtimes/available': [available('e2b-cloud', 'E2B Cloud', { ...CLOUD_FORM,
        edit_blocker: { code: 'needs_domain', message: "E2B Cloud's sandboxes call this server back at its login address: connect a domain first (the server address step, or Settings → General)", sandboxes: [] } })],
      'GET /api/runtimes/change': IDLE,
      'GET /api/runtimes': { default: '', runtimes: [] },
    })
    renderApp(<SetupPage />, '/app/setup')
    const user = userEvent.setup()
    expect((await screen.findByRole('alert')).textContent).toContain('Needs a domain first.')
    expect(screen.queryByRole('form', { name: 'E2B Cloud settings' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Choose a domain' }))
    expect(await screen.findByRole('heading', { name: 'Where will this server answer?' })).toBeTruthy()
  })
})

describe('a runtime in one line', () => {
  it('says its plan and how many run of how many, or why it needs attention', () => {
    const hobby = runtime('e2b-cloud', 'E2B Cloud', { plan: { name: 'hobby', title: 'Hobby' }, limits: { sandboxes: 10 },
      status: { state: 'connected', summary: 'E2B Cloud · 0 running here (the plan runs 10 at once per E2B team)', running: 0 } })
    expect(runtimeLine(hobby)).toBe('Hobby · 0 of 10 running')
    expect(runtimeLine({ ...hobby, status: { state: 'attention', summary: 'E2B Cloud did not answer' } })).toBe('E2B Cloud did not answer')
    expect(runtimeLine({ ...EMBED, status: { state: 'connected', summary: 'E2B Embed · 3 running', running: 3 } })).toBe('On this server · 3 running')
  })
})
