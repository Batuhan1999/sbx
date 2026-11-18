import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { setSetupToken } from '@/api/client'
import type { SetupState } from '@/api/types'
import { isMarkedReady, markReady } from '@/lib/ready'
import ReposPage from '@/pages/repos/ReposPage'
import SetupPage from '@/pages/setup/SetupPage'
import { ME, renderApp, stubApi } from './render'

type StepName = SetupState['steps'][number]['step']
const state = (done: StepName[] = [], over: Partial<SetupState> = {}, access: Partial<SetupState['access']> = {}): SetupState => ({
  complete: false,
  steps: (['address', 'sign_in', 'runtime', 'agents'] as const).map(step => ({ step, done: done.includes(step), optional: false, summary: '' })),
  signed_in: false, email: null, open_access: false,
  agents: [{ kind: 'amp', title: 'Amp', chosen: false }, { kind: 'codex', title: 'Codex', chosen: false }],
  can_complete: false, host: { address: '203.0.113.7' },
  access: { mode: 'ip', domain: '', url: '', certificate: 'delegated', certificate_email: '', certificate_ready: false, certificate_error: '', needs_ports: [53], ...access },
  link: '', admins: [], version: 'dev', ...over,
})
const SAVED = { mode: 'domain' as const, domain: 'sbx.example.com', url: 'https://app.sbx.example.com' }
const READY = { ...SAVED, certificate_ready: true }

const runtime = (kind: string, title: string, over: Record<string, unknown> = {}) => ({
  kind, title, host: false, enabled: false, default: false, can_run: true, why_not: '', managed_by: '', edit_blocker: null,
  settings: [{ key: `${kind.toUpperCase()}_API_KEY`, label: `${title} API key`, secret: true, default: '', help: '', kind: 'text', optional: false, limit: '' }],
  presets: [{ name: '20', title: '$20', values: {}, source: 'https://provider.example/plans' }],
  values: {}, secrets_set: [], facts: {}, sandboxes: [], remove_blocker: null, limit_hit: null, plan: null, ...over,
})
const EMBED = runtime('embed', 'E2B Embed', { host: true, enabled: true, default: true, managed_by: 'install.sh', settings: [], presets: [], facts: { kvm: true, memory_gb: 64 } })
const NO_KVM = runtime('embed', 'E2B Embed', { host: true, can_run: false, why_not: 'needs /dev/kvm', settings: [], presets: [] })
const IDLE = { state: 'idle', message: '', at: '2026-11-18T10:00:00Z', operation: '', kind: '' }
const TEMPLATES = { blocked: '', runtimes: { embed: [{ key: 'sbx-base', phase: 'current', status: 'built', template: 't1', error: null }, { key: 'default', phase: 'building', status: 'building', template: null, error: null }] } }
const check = (name: string, ok: boolean, detail: string, fix = '') => ({ name, ok, detail, fix })

const steps = () => within(screen.getByRole('list', { name: 'Steps' })).getAllByRole('listitem').map(li => li.textContent)

describe('server setup', () => {
  it('walks Domain, Sandboxes, Account, GitHub in that order, the same row on every step', async () => {
    setSetupToken('one-time-token')
    stubApi({ 'GET /api/setup': state() })
    const first = renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Where will this server answer?' })).toBeTruthy()
    // The onboarding link's token stands in for an admin until the account exists.
    expect(((vi.mocked(fetch).mock.calls[0][1] as RequestInit).headers as Record<string, string>).authorization).toBe('Setup one-time-token')
    expect(steps()).toEqual(['1Domain', '2Sandboxes', '3Account', '4GitHub'])
    expect(screen.getByText(/Step 1 of 4/).textContent).toBe('Step 1 of 4 · Domain')
    expect(screen.getByRole('button', { name: /Domain/ }).getAttribute('aria-current')).toBe('step')
    // A step not reached yet can't be jumped to.
    expect((screen.getByRole('button', { name: /Account/ }) as HTMLButtonElement).disabled).toBe(true)
    first.unmount()

    stubApi({ 'GET /api/setup': state(['address', 'sign_in', 'runtime', 'agents'], { complete: true, signed_in: true }, SAVED), 'GET /api/connections': { connections: [] } })
    renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Connect GitHub' })).toBeTruthy()
    expect(steps().map(t => t?.replace(/^\d/, ''))).toEqual(['Domain', 'Sandboxes', 'Account', 'GitHub'])
    expect(screen.getByText(/Step 4 of 4/).textContent).toBe('Step 4 of 4 · GitHub')
    expect(within(screen.getByRole('list', { name: 'Steps' })).getByRole('button', { name: /GitHub/ }).getAttribute('aria-current')).toBe('step')
  })

  it('shows the three records and the checks that failed, port 53 with its fix', async () => {
    const user = userEvent.setup()
    const name = 'sbx.example.com'
    const calls = stubApi({
      'GET /api/setup': state(),
      'PUT /api/connections/domain': () => ({ status: 400, body: { detail: { message: 'Check the DNS records below.', checks: [
        check(`app.${name} resolves`, true, '203.0.113.7'),
        check('delegation', false, 'no NS record yet', `an NS record: _acme-challenge.${name} NS ${name}.`),
        check('port 53', false, 'a query from outside timed out', 'allow TCP and UDP port 53 in your provider’s firewall'),
      ] } } }),
    })
    renderApp(<SetupPage />)
    await user.type(await screen.findByLabelText('Domain'), 'SBX.example.com')
    const records = within(screen.getByRole('region', { name: 'DNS records' }))
    expect(records.getByText(`*.${name}`)).toBeTruthy()
    expect(records.getByText(`_acme-challenge.${name}`)).toBeTruthy()
    expect(records.getByText(`${name}.`)).toBeTruthy()
    expect(records.getAllByText('203.0.113.7')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Check records' }))
    const failed = within(await screen.findByRole('alert'))
    expect(failed.getByText('2 checks failed')).toBeTruthy()
    expect(failed.getByText('port 53')).toBeTruthy()
    expect(failed.getByText(/allow TCP and UDP port 53 in your provider’s firewall/)).toBeTruthy()
    expect(failed.getByText('no NS record yet')).toBeTruthy()
    expect(failed.getByText('1 passed')).toBeTruthy()
    // Only the delegated method, and no way on until the records pass.
    expect(calls.find(c => c.method === 'PUT')!.body).toEqual({ values: { name, address: '203.0.113.7', acme: 'delegated' } })
    expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
  })

  it('lets the owner continue while the certificate comes, and says when it failed', async () => {
    stubApi({ 'GET /api/setup': state([], {}, SAVED) })
    const user = userEvent.setup()
    const waiting = renderApp(<SetupPage />)
    await user.click(await screen.findByRole('button', { name: /Domain/ }))   // a saved domain opens on the next step
    expect(await screen.findByText(/Records found. The certificate is on its way/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Continue/ })).toBeTruthy()
    waiting.unmount()

    stubApi({ 'GET /api/setup': state([], {}, { ...SAVED, certificate_error: 'the challenge was not answered' }) })
    renderApp(<SetupPage />)
    await user.click(await screen.findByRole('button', { name: /Domain/ }))
    const failed = (await screen.findByRole('alert')).textContent
    expect(failed).toContain('Couldn’t get the certificate')
    expect(failed).toContain('the challenge was not answered')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Continue/ })).toBeTruthy()
  })

  it('shows a server that runs sandboxes itself as chosen, with a provider as an extra', async () => {
    const user = userEvent.setup()
    const calls = stubApi({
      'GET /api/setup': state(['address', 'runtime'], {}, SAVED),
      'GET /api/runtimes/available': [EMBED, runtime('boat', 'Boat')],
      'GET /api/runtimes/change': IDLE,
      'GET /api/templates': TEMPLATES,
      'GET /api/connections': { connections: [] },
      'POST /api/runtimes': { ...IDLE, state: 'queued', operation: 'add', kind: 'boat' },
    })
    renderApp(<SetupPage />)
    await user.click(await screen.findByRole('button', { name: /Sandboxes/ }))
    expect(await screen.findByText('This server can run sandboxes itself, so it will.')).toBeTruthy()
    const chosen = within(screen.getByRole('list', { name: 'Chosen' }))
    expect(chosen.getByText('On this server')).toBeTruthy()
    expect(chosen.getByText(/64 GiB of memory: room for about 13 sandboxes/)).toBeTruthy()
    expect(screen.queryByLabelText('Boat API key')).toBeNull()
    expect(await screen.findByText(/Building templates: 1 of 2 ready/)).toBeTruthy()
    expect((screen.getByRole('button', { name: /Continue/ }) as HTMLButtonElement).disabled).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Also add a provider for more room' }))
    await user.type(screen.getByLabelText('Boat API key'), 'test-only-key')
    expect(screen.queryByText(/\$20/)).toBeNull()   // no plan to pick: the server reads it from the key
    await user.click(screen.getByRole('button', { name: 'Add Boat' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({ kind: 'boat', values: { BOAT_API_KEY: 'test-only-key' } }))
  })

  it('asks a server without /dev/kvm for a provider key only, and shows a refused key', async () => {
    const user = userEvent.setup()
    stubApi({
      'GET /api/setup': state(['address'], {}, SAVED),
      'GET /api/runtimes/available': [NO_KVM, runtime('boat', 'Boat'), runtime('e2b', 'E2B Cloud')],
      'GET /api/runtimes/change': IDLE,
      'POST /api/runtimes': () => ({ status: 400, body: { detail: 'Boat refused the key: 401 Unauthorized.' } }),
    })
    renderApp(<SetupPage />)
    expect(await screen.findByText(/This server can’t run sandboxes itself/)).toBeTruthy()
    expect(screen.getAllByRole('radio').map(r => (r as HTMLInputElement).value)).toEqual(['boat', 'e2b'])
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
    await user.type(screen.getByLabelText('Boat API key'), 'test-only-bad')
    await user.click(screen.getByRole('button', { name: 'Check key' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Boat refused the key')
    expect(document.body.textContent).not.toContain('test-only')
  })

  it('creates the account and finishes from the button under the form', async () => {
    const user = userEvent.setup()
    const ready = state(['address', 'runtime'], {}, READY)   // no agents step: the account follows the sandboxes
    const calls = stubApi({
      'GET /api/setup': ready,
      'GET /api/templates': TEMPLATES,
      'GET /api/connections': { connections: [] },
      'POST /api/setup/admin': { ...ready, signed_in: true, email: 'ada@example.com' },
      'POST /api/setup/complete': { ...ready, signed_in: true, email: 'ada@example.com', complete: true },
    })
    renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Create your account' })).toBeTruthy()
    const button = screen.getByRole('button', { name: 'Create account and finish' })
    // The regression: the button sits outside its <form>, so it must name the form it submits.
    expect(button.closest('form')).toBeNull()
    expect(button.getAttribute('form')).toBe(document.querySelector('form')!.id)
    await user.click(button)   // empty: nothing is sent, the fields say why
    expect(await screen.findByText('Repeat the password.')).toBeTruthy()
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'test-only-pass')
    await user.type(screen.getByLabelText('Repeat password'), 'test-only-pass')
    await user.click(button)
    expect(await screen.findByRole('heading', { name: 'Connect GitHub' })).toBeTruthy()
    expect(calls.filter(c => c.method === 'POST').map(c => [c.url, c.body])).toEqual([
      ['/api/setup/admin', { email: 'ada@example.com', password: 'test-only-pass' }],
      ['/api/setup/complete', undefined],
    ])
    expect(isMarkedReady()).toBe(true)
    expect(screen.getByRole('button', { name: 'Skip for now' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Create the app on GitHub/ })).toBeTruthy()
  })
})

describe('the account waits for the certificate', () => {
  it('holds the form until the certificate is ready, and retries one that failed', async () => {
    const user = userEvent.setup()
    stubApi({ 'GET /api/setup': state(['address', 'runtime'], {}, SAVED), 'GET /api/templates': TEMPLATES })
    const pending = renderApp(<SetupPage />)
    expect(await screen.findByText('Waiting for the certificate for sbx.example.com, usually under a minute')).toBeTruthy()
    expect((screen.getByLabelText('Email') as HTMLInputElement).matches(':disabled')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Create account and finish' })).toBeNull()
    pending.unmount()

    const calls = stubApi({
      'GET /api/setup': state(['address', 'runtime'], {}, { ...SAVED, certificate_error: 'the challenge was not answered' }),
      'GET /api/templates': TEMPLATES, 'PUT /api/connections/domain': { state: 'connected' },
    })
    renderApp(<SetupPage />)
    const failed = (await screen.findByRole('alert')).textContent
    expect(failed).toContain('Couldn’t get the certificate for sbx.example.com')
    expect(failed).toContain('the challenge was not answered')
    expect(screen.queryByRole('button', { name: 'Create account and finish' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(calls.find(c => c.method === 'PUT')?.body).toEqual({ values: { name: 'sbx.example.com', address: '203.0.113.7', acme: 'delegated' } }))
  })
})

describe('the ready dialog', () => {
  const answers = {
    'GET /api/me': { ...ME, base_url: 'https://app.sbx.example.com', repos: [], agents: { amp: { title: 'Amp', command: 'amp' } } },
    'GET /api/repos': { repos: [] },
    'GET /api/runtimes': { default: 'embed', runtimes: [{ kind: 'embed', title: 'E2B Embed' }] },
    'GET /api/connections': { connections: [{ kind: 'github', title: 'GitHub', group: 'platform', state: 'not_connected', summary: '', display: [], facts: {}, actions: [], fields: [] }] },
    'GET /api/templates': TEMPLATES,
  }

  it('sums up the server once, right after the setup', async () => {
    stubApi(answers)
    markReady()
    const first = renderApp(<ReposPage />)
    const dialog = within(await screen.findByRole('dialog', { name: 'Your server is ready' }))
    expect(dialog.getByText('app.sbx.example.com')).toBeTruthy()
    expect(await dialog.findByText('E2B Embed')).toBeTruthy()
    expect(await dialog.findByText(/Not connected/)).toBeTruthy()
    expect(await dialog.findByText(/Building templates: 1 of 2 ready/)).toBeTruthy()
    // Agents are not chosen in the setup: running their cloud sessions here is an optional next step.
    const sessions = within(dialog.getByRole('region', { name: 'Run agent sessions here' }))
    expect(sessions.getAllByRole('link').map(a => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Run Amp threads in sbx sandboxesSet up Amp', '/app/extensions'], ['Run Claude Code cloud sessions hereSet up Claude Code', '/app/extensions']])
    expect(sessions.getByText('Codex is included: each developer uses their own login.')).toBeTruthy()
    expect(dialog.getByRole('button', { name: 'Look around' })).toBeTruthy()
    expect(dialog.getByRole('link', { name: /Connect a repository/ }).getAttribute('href')).toBe('/app/repos/new')
    first.unmount()

    renderApp(<ReposPage />)
    expect(await screen.findByText('Connect your first repository')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('closes on "Look around" and stays away on a server that was not just set up', async () => {
    const user = userEvent.setup()
    stubApi(answers)
    const plain = renderApp(<ReposPage />)
    expect(await screen.findByText('Connect your first repository')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    plain.unmount()

    markReady()
    renderApp(<ReposPage />)
    await user.click(await screen.findByRole('button', { name: 'Look around' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(isMarkedReady()).toBe(false)
  })
})

describe('a certificate that failed', () => {
  it('names a blocked port 53 and where to open it', async () => {
    const { CertificateError } = await import('@/pages/setup/SetupPage')
    const { render } = await import('@testing-library/react')
    const view = render(<CertificateError domain="sbx.example.com" error="port 53: no public resolver (1.1.1.1, 8.8.8.8) reached this server on port 53 within 5 s; fix: allow TCP and UDP port 53 in your provider's firewall" />)
    const text = view.getByRole('alert').textContent ?? ''
    expect(text).toContain('Port 53 is blocked')
    expect(text).toContain('TCP and UDP port 53')
  })
})
