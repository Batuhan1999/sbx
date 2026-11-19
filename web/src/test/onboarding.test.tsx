import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SetupOpen, SetupState } from '@/api/types'
import { TemplateProgressLine } from '@/components/TemplateProgressLine'
import { browser } from '@/lib/browser'
import ReposPage from '@/pages/repos/ReposPage'
import SetupPage from '@/pages/setup/SetupPage'
import { ME, renderApp, stubApi } from './render'

type StepName = SetupState['steps'][number]['step']
const state = (done: StepName[] = [], over: Partial<SetupState> = {}, access: Partial<SetupState['access']> = {}): SetupState => ({
  complete: false,
  steps: (['address', 'sign_in', 'runtime', 'agents'] as const).map(step => ({ step, done: step === 'sign_in' || done.includes(step), optional: false, summary: '' })),
  signed_in: true, email: 'ada@example.com', open_access: false,
  agents: [{ kind: 'amp', title: 'Amp', chosen: false }, { kind: 'codex', title: 'Codex', chosen: false }],
  can_complete: false, host: { address: '203.0.113.7' },
  access: { mode: 'ip', domain: '', url: '', certificate: 'delegated', certificate_email: '', certificate_ready: false, certificate_error: '', needs_ports: [53], ...access },
  link: '', admins: ['ada@example.com'], version: 'dev', ...over,
})
const SAVED = { mode: 'domain' as const, domain: 'sbx.example.com', url: 'https://app.sbx.example.com' }
const READY = { ...SAVED, certificate_ready: true }
/** What anyone may know. The page asks it first: its answer comes before the state's in every stub (a prefix match). */
const open = (over: Partial<SetupOpen> = {}): SetupOpen => ({ complete: false, account: true, signed_in: true, link: `${location.origin}/onboarding`, version: 'dev', ...over })
const FRESH = open({ account: false, signed_in: false })
/** The stubs of a signed-in admin's page. */
const admin = (answers: Parameters<typeof stubApi>[0]) => stubApi({ 'GET /api/setup/open': open(), ...answers })

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
afterEach(() => { vi.restoreAllMocks(); history.replaceState(null, '', '/') })

describe('the onboarding’s first step', () => {
  it('opens on the account for a server nobody can sign in to, with no credential', async () => {
    stubApi({ 'GET /api/setup/open': FRESH })
    renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Create your account' })).toBeTruthy()
    // Nothing stands in for an admin: the page carries no token, and asks only what anyone may know.
    const asked = vi.mocked(fetch).mock.calls.map(c => [c[0], ((c[1] as RequestInit).headers as Record<string, string>).authorization])
    expect(asked).toEqual([['/api/setup/open', undefined]])
    expect(steps()).toEqual(['1Account', '2Domain', '3Sandboxes', '4GitHub'])
    expect(screen.getByText(/Step 1 of 4/).textContent).toBe('Step 1 of 4 · Account')
    expect(screen.getByRole('button', { name: /Account/ }).getAttribute('aria-current')).toBe('step')
    // The form is open at once, and a step not reached yet can't be jumped to.
    expect((screen.getByLabelText('Email') as HTMLInputElement).matches(':disabled')).toBe(false)
    expect((screen.getByRole('button', { name: /Domain/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('creates the account from the button under the form and goes on to the domain', async () => {
    const user = userEvent.setup()
    let made = false
    const calls = stubApi({
      'GET /api/setup/open': () => ({ status: 200, body: made ? open() : FRESH }),
      'GET /api/setup': state(),
      'POST /api/setup/admin': () => { made = true; return { status: 200, body: state() } },
    })
    renderApp(<SetupPage />)
    const button = await screen.findByRole('button', { name: 'Create account' })
    // The regression: the button sits outside its <form>, so it must name the form it submits.
    expect(button.closest('form')).toBeNull()
    expect(button.getAttribute('form')).toBe(document.querySelector('form')!.id)
    await user.click(button)   // empty: nothing is sent, the fields say why
    expect(await screen.findByText('Repeat the password.')).toBeTruthy()
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'test-only-pass')
    await user.type(screen.getByLabelText('Repeat password'), 'test-only-pass')
    await user.click(button)
    expect(await screen.findByRole('heading', { name: 'Where will this server answer?' })).toBeTruthy()
    expect(calls.filter(c => c.method === 'POST').map(c => [c.url, c.body])).toEqual([['/api/setup/admin', { email: 'ada@example.com', password: 'test-only-pass' }]])
    expect(steps().map(t => t?.replace(/^\d/, ''))).toEqual(['Account', 'Domain', 'Sandboxes', 'GitHub'])
    expect(screen.getByText(/Step 2 of 4/).textContent).toBe('Step 2 of 4 · Domain')
    // The account is made: there is no way back to it.
    expect((screen.getByRole('button', { name: /Account/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('says so when someone else made the account first', async () => {
    const user = userEvent.setup()
    let taken = false
    stubApi({
      'GET /api/setup/open': () => ({ status: 200, body: taken ? open({ signed_in: false }) : FRESH }),
      'POST /api/setup/admin': () => { taken = true; return { status: 409, body: { detail: 'This server already has an admin. Sign in to go on.' } } },
    })
    renderApp(<SetupPage />)
    await user.type(await screen.findByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'test-only-pass')
    await user.type(screen.getByLabelText('Repeat password'), 'test-only-pass')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('heading', { name: 'Sign in to go on' })).toBeTruthy()
  })

  it('asks an admin who is not signed in to sign in, where the onboarding is now', async () => {
    stubApi({ 'GET /api/setup/open': open({ signed_in: false }) })
    const here = renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Sign in to go on' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/login?next=%2Fonboarding')
    expect(screen.queryByLabelText('Email')).toBeNull()
    here.unmount()

    // Opened at the IP address after the server moved to its domain: the page is there now.
    stubApi({ 'GET /api/setup/open': open({ signed_in: false, link: 'https://app.sbx.example.com/onboarding' }) })
    renderApp(<SetupPage />)
    expect((await screen.findByRole('link', { name: 'Open app.sbx.example.com' })).getAttribute('href')).toBe('https://app.sbx.example.com/onboarding')
  })
})

describe('the domain step', () => {
  it('shows the three records and the checks that failed, port 53 with its fix', async () => {
    const user = userEvent.setup()
    const name = 'sbx.example.com'
    const calls = admin({
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

  it('waits for the certificate, and retries one that failed', async () => {
    const user = userEvent.setup()
    admin({ 'GET /api/setup': state([], {}, SAVED) })
    const waiting = renderApp(<SetupPage />)
    expect(await screen.findByText('Records found. Getting the certificate for sbx.example.com, usually under a minute.')).toBeTruthy()
    // Nothing to press while it comes, and no step ahead to jump to.
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
    expect((screen.getByRole('button', { name: /Sandboxes/ }) as HTMLButtonElement).disabled).toBe(true)
    waiting.unmount()

    const calls = admin({
      'GET /api/setup': state([], {}, { ...SAVED, certificate_error: 'the challenge was not answered' }),
      'PUT /api/connections/domain': { state: 'connected' },
    })
    renderApp(<SetupPage />)
    const failed = (await screen.findByRole('alert')).textContent
    expect(failed).toContain('Couldn’t get the certificate')
    expect(failed).toContain('the challenge was not answered')
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(calls.find(c => c.method === 'PUT')?.body).toEqual({ values: { name: 'sbx.example.com', address: '203.0.113.7', acme: 'delegated' } }))
  })

  it('goes on with Continue when the page is at the server’s own address already', async () => {
    const user = userEvent.setup()
    admin({
      'GET /api/setup': state(['address'], { link: `${location.origin}/onboarding` }, READY),
      'GET /api/runtimes/available': [NO_KVM, runtime('boat', 'Boat')], 'GET /api/runtimes/change': IDLE,
    })
    renderApp(<SetupPage />)
    // A saved domain whose certificate is ready opens on the next step; the domain can still be looked at.
    expect(await screen.findByRole('heading', { name: 'Where will sandboxes run?' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /Domain/ }))
    expect(await screen.findByText('Certificate ready for sbx.example.com.')).toBeTruthy()
    expect(screen.queryByText(/Moving to/)).toBeNull()
    await user.click(screen.getByRole('button', { name: /Continue/ }))
    expect(await screen.findByRole('heading', { name: 'Where will sandboxes run?' })).toBeTruthy()
  })
})

describe('the move to the server’s own address', () => {
  // The control plane answers at app.<domain>, which the state's link names; the page was opened at the IP address.
  const moving = (domain: string) => state(['address'], { link: `https://app.${domain}/onboarding` }, { ...READY, domain, url: `https://app.${domain}` })

  it('moves once that address answers, with a code that signs the admin in there', async () => {
    const assign = vi.spyOn(browser, 'assign').mockImplementation(() => {})
    const calls = admin({ 'GET /api/setup': moving('sbx.example.com'), 'POST /api/setup/handoff': { code: 'one-time-code' } })
    renderApp(<SetupPage />)
    expect(await screen.findByText('Certificate ready. Moving to app.sbx.example.com…')).toBeTruthy()
    // It asks the address the link names (app.<domain>, never the bare domain), and moves on its answer.
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://app.sbx.example.com/onboarding#h=one-time-code'))
    expect(vi.mocked(fetch).mock.calls.map(c => c[0])).toContain('https://app.sbx.example.com/dist/version')
    expect(assign).toHaveBeenCalledTimes(1)
    expect(calls.filter(c => c.url === '/api/setup/handoff')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
  })

  it('offers the address as a button when it does not answer, and moves without a code when none is given', async () => {
    const assign = vi.spyOn(browser, 'assign').mockImplementation(() => {})
    admin({ 'GET /api/setup': moving('slow.example.com'), 'POST /api/setup/handoff': () => ({ status: 401, body: { detail: 'sign in' } }) })
    const answered = vi.mocked(fetch).getMockImplementation()!
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => url.includes('/dist/version') ? Promise.reject(new TypeError('blocked')) : answered(url, init)))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderApp(<SetupPage />)
      expect(await screen.findByText('Certificate ready. Moving to app.slow.example.com…')).toBeTruthy()
      expect(screen.queryByRole('button', { name: /Open app\.slow\.example\.com/ })).toBeNull()
      await act(() => vi.advanceTimersByTimeAsync(10_000))
      const button = await screen.findByRole('button', { name: /Open app\.slow\.example\.com/ })
      expect(assign).not.toHaveBeenCalled()
      await act(async () => { button.click() })
      // No code (the session at the IP address ended): the owner signs in at the new address.
      await waitFor(() => expect(assign).toHaveBeenCalledWith('https://app.slow.example.com/onboarding'))
    } finally { vi.useRealTimers() }
  })

  it('signs in with the code it arrives with, and takes it out of the address', async () => {
    history.replaceState(null, '', '/onboarding#h=one-time-code')
    let entered = false
    const calls = stubApi({
      'GET /api/setup/open': () => ({ status: 200, body: open({ signed_in: entered }) }),
      'POST /api/setup/enter': () => { entered = true; return { status: 200, body: open() } },
      'GET /api/setup': state(['address', 'runtime'], { link: `${location.origin}/onboarding` }, READY),
      'GET /api/connections': { connections: [] }, 'GET /api/templates': TEMPLATES,
    })
    renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Connect GitHub' })).toBeTruthy()
    expect(calls[0]).toEqual({ method: 'POST', url: '/api/setup/enter', body: { code: 'one-time-code' } })   // before it asks who this is
    expect(location.hash).toBe('')
  })

  it('asks to sign in when the code it arrives with is spent', async () => {
    history.replaceState(null, '', '/onboarding#h=spent-code')
    stubApi({
      'GET /api/setup/open': open({ signed_in: false }),
      'POST /api/setup/enter': () => ({ status: 403, body: { detail: 'That sign-in has expired. Sign in to go on.' } }),
    })
    renderApp(<SetupPage />)
    expect(await screen.findByRole('heading', { name: 'Sign in to go on' })).toBeTruthy()
    expect(location.hash).toBe('')
  })
})

describe('the sandboxes step', () => {
  it('says why a server with too little memory runs its sandboxes at a provider', async () => {
    const why = 'needs 16 GiB of memory: 12 for the host and 4 for one sandbox; this server has 8 GiB'
    admin({
      'GET /api/setup': state(['address'], {}, READY),
      'GET /api/runtimes/available': [{ ...NO_KVM, why_not: why, facts: { kvm: true, memory_gb: 8 } }, runtime('boat', 'Boat')],
      'GET /api/runtimes/change': IDLE,
    })
    renderApp(<SetupPage />)
    expect((await screen.findByText(/This server can’t run sandboxes itself/)).textContent).toContain(`(E2B Embed ${why})`)
    expect(document.body.textContent).not.toContain('no /dev/kvm')
  })

  it('links each key to where the provider hands it out', async () => {
    admin({
      'GET /api/setup': state(['address'], {}, READY),
      'GET /api/runtimes/available': [NO_KVM, runtime('boat', 'Boat', { settings: [{ key: 'BOAT_API_KEY', label: 'Boat API key', secret: true, default: '', help: 'From Boat’s dashboard: API keys.', help_url: 'https://boat.dev/dashboard', kind: 'text', optional: false, limit: '' }] })],
      'GET /api/runtimes/change': IDLE,
    })
    renderApp(<SetupPage />)
    const link = await screen.findByRole('link', { name: /Get one at boat\.dev\/dashboard/ })
    expect(link.getAttribute('href')).toBe('https://boat.dev/dashboard')
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('keeps the key’s form busy, and nothing else, until the server runs the provider', async () => {
    admin({
      'GET /api/setup': state(['address', 'runtime'], {}, READY),
      'GET /api/runtimes/available': [NO_KVM, runtime('boat', 'Boat', { enabled: true })],
      'GET /api/runtimes/change': { ...IDLE, state: 'done', operation: 'add', kind: 'boat', restarting: true },
      'GET /api/templates': TEMPLATES, 'GET /api/connections': { connections: [] },
    })
    const user = userEvent.setup()
    renderApp(<SetupPage />)
    await user.click(await screen.findByRole('button', { name: /Sandboxes/ }))
    const check = await screen.findByRole('button', { name: /Check key/ })   // added, the server restarting: one step still
    expect(check.getAttribute('aria-busy')).toBe('true')
    expect((screen.getByLabelText('Boat API key') as HTMLInputElement).disabled).toBe(true)
    expect(screen.queryByRole('list', { name: 'Chosen' })).toBeNull()       // no card until it runs
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
    expect(screen.queryByText(/restarts|Checking the key/)).toBeNull()      // no second loader
  })

  it('shows a server that runs sandboxes itself as chosen, with a provider as an extra', async () => {
    const user = userEvent.setup()
    const calls = admin({
      'GET /api/setup': state(['address', 'runtime'], {}, READY),
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
    admin({
      'GET /api/setup': state(['address'], {}, READY),
      'GET /api/runtimes/available': [NO_KVM, runtime('boat', 'Boat'), runtime('e2b', 'E2B Cloud')],
      'GET /api/runtimes/change': IDLE,
      'POST /api/runtimes': () => ({ status: 400, body: { detail: 'Boat refused the key: 401 Unauthorized.' } }),
    })
    renderApp(<SetupPage />)
    expect(await screen.findByText(/This server can’t run sandboxes itself \(E2B Embed needs \/dev\/kvm\), so they run at a provider/)).toBeTruthy()
    expect(screen.getAllByRole('radio').map(r => (r as HTMLInputElement).value)).toEqual(['boat', 'e2b'])
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
    await user.type(screen.getByLabelText('Boat API key'), 'test-only-bad')
    await user.click(screen.getByRole('button', { name: 'Check key' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Boat refused the key')
    expect(document.body.textContent).not.toContain('test-only')
  })
})

const Where = () => <output aria-label="where">{useLocation().pathname}</output>
const GITHUB = (state: string) => ({ connections: [{ kind: 'github', title: 'GitHub', group: 'platform', state, summary: state === 'connected' ? 'App sbx-acme' : '', display: [], facts: {}, actions: [], fields: [] }] })

describe('the GitHub step leads to the control plane', () => {
  const last = state(['address', 'runtime'], { link: `${location.origin}/onboarding` }, READY)
  const stubs = (github = 'not_connected') => stubApi({
    'GET /api/setup/open': open(), 'GET /api/setup': last, 'GET /api/templates': TEMPLATES, 'GET /api/connections': GITHUB(github),
    'POST /api/connections/github/manifest': { action_url: 'https://github.com/settings/apps/new?state=n', manifest: '{}' },
  })

  it('skipping GitHub opens the control plane, without finishing the onboarding', async () => {
    const user = userEvent.setup()
    const calls = stubs()
    renderApp(<><SetupPage /><Where /></>, '/onboarding')
    expect(await screen.findByRole('heading', { name: 'Connect GitHub' })).toBeTruthy()
    expect(screen.getByText(/Step 4 of 4/).textContent).toBe('Step 4 of 4 · GitHub')
    await user.click(screen.getByRole('button', { name: 'Skip for now' }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/repos'))
    expect(calls.filter(c => c.method === 'POST')).toEqual([])   // the summary there finishes it
  })

  it('connects GitHub from its own card, not from the step’s buttons, and hands the browser to GitHub', async () => {
    const user = userEvent.setup()
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
    const calls = stubs()
    renderApp(<SetupPage />, '/onboarding')
    expect(await screen.findByText('Not connected')).toBeTruthy()
    expect(screen.queryByLabelText(/Organization/)).toBeNull()               // only on request
    const nav = within(screen.getByRole('button', { name: 'Skip for now' }).parentElement!)
    expect(nav.queryByRole('button', { name: /Connect GitHub/ })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Connect GitHub' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(calls.filter(c => c.method === 'POST').map(c => [c.url, c.body])).toEqual([
      ['/api/connections/github/manifest', { owner: '', return_to: '/app/repos' }]])   // your own account
  })

  it('creates the App for an organization when asked', async () => {
    const user = userEvent.setup()
    vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
    const calls = stubs()
    renderApp(<SetupPage />, '/onboarding')
    await user.click(await screen.findByRole('button', { name: 'For an organization?' }))
    await user.type(screen.getByLabelText('Organization'), 'acme')
    await user.click(screen.getByRole('button', { name: 'Connect GitHub' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({ owner: 'acme', return_to: '/app/repos' })
  })

  it('goes on with Continue once GitHub is connected', async () => {
    const user = userEvent.setup()
    stubs('connected')
    renderApp(<><SetupPage /><Where /></>, '/onboarding')
    expect(await screen.findByLabelText('connected')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Skip for now' })).toBeNull()
    await user.click(screen.getByRole('button', { name: /Continue/ }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/repos'))
  })
})

describe('the summary over the control plane, the onboarding’s last step', () => {
  const ready = state(['address', 'runtime'], { can_complete: true }, READY)
  const stubs = (complete: () => { status: number; body: unknown }, over: { github?: string; setup?: SetupState } = {}) => {
    let done = false
    return stubApi({
      'GET /api/me': () => ({ status: 200, body: { ...ME, base_url: 'https://app.sbx.example.com', setup_complete: done } }),
      'GET /api/setup': () => ({ status: 200, body: { ...(over.setup ?? ready), complete: done } }),
      'POST /api/setup/complete': () => { const answer = complete(); done = answer.status < 400; return answer },
      // As the server does: only the onboarding until it is finished.
      'GET /api/repos': () => done ? { status: 200, body: { repos: [] } }
        : { status: 503, body: { detail: 'This installation is being set up. Finish at /onboarding.' } },
      'GET /api/templates': TEMPLATES, 'GET /api/connections': GITHUB(over.github ?? 'not_connected'),
      'GET /api/runtimes': { default: 'boat', runtimes: [{ kind: 'boat', title: 'Boat' }] },
      'GET /api/github/repositories': { repositories: [] },
    })
  }
  const finishes = () => ({ status: 200, body: { ...ready, complete: true } })

  it('sums up the server, with the optional agent sessions, until something is chosen', async () => {
    const calls = stubs(finishes)
    renderApp(<ReposPage />)
    const dialog = within(await screen.findByRole('dialog', { name: 'Your server is ready' }))
    expect(dialog.getByText('app.sbx.example.com')).toBeTruthy()
    expect(await dialog.findByText('Boat')).toBeTruthy()
    expect(await dialog.findByText(/Not connected/)).toBeTruthy()
    expect(await dialog.findByText(/Building templates: 1 of 2 ready/)).toBeTruthy()
    const sessions = within(dialog.getByRole('region', { name: 'Run agent sessions here' }))
    expect(sessions.getAllByRole('button').map(b => b.textContent)).toEqual([
      'Run Amp threads in sbx sandboxesSet up Amp', 'Run Claude Code cloud sessions hereSet up Claude Code'])
    expect(sessions.getByText('Codex is included: each developer uses their own login.')).toBeTruthy()
    // The page behind it waits for the onboarding to finish rather than showing the server's refusal.
    expect(calls.some(c => c.url === '/api/repos')).toBe(false)
    expect(screen.queryByText(/being set up/)).toBeNull()
  })

  it('finishes the onboarding with Connect a repository, and goes there', async () => {
    const user = userEvent.setup()
    const calls = stubs(finishes, { github: 'connected' })
    renderApp(<><ReposPage /><Where /></>, '/app/repos')
    const dialog = within(await screen.findByRole('dialog', { name: 'Your server is ready' }))
    expect(await dialog.findByText('App sbx-acme')).toBeTruthy()
    // The connect page's list is asked for while the summary shows: the page opens with it.
    await waitFor(() => expect(calls.some(c => c.url === '/api/github/repositories')).toBe(true))
    await user.click(dialog.getByRole('button', { name: /Connect a repository/ }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/repos/new'))
    expect(calls.filter(c => c.method === 'POST').map(c => c.url)).toEqual(['/api/setup/complete'])
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('finishes it with Look around and with a set-up link, too', async () => {
    const user = userEvent.setup()
    stubs(finishes)
    const first = renderApp(<ReposPage />)
    await user.click(await screen.findByRole('button', { name: 'Look around' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(await screen.findByText('Start your first sandbox')).toBeTruthy()
    first.unmount()

    stubs(finishes)
    renderApp(<><ReposPage /><Where /></>, '/app/repos')
    await user.click(await screen.findByRole('button', { name: /Set up Amp/ }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/extensions'))
  })

  it('stays with the server’s checks when finishing is refused', async () => {
    const user = userEvent.setup()
    stubs(() => ({ status: 400, body: { detail: { message: 'The server needs attention before setup can finish.', checks: [check('Boat API', false, 'the key was revoked')] } } }))
    renderApp(<ReposPage />)
    await user.click(await screen.findByRole('button', { name: /Connect a repository/ }))
    const alert = (await screen.findByRole('alert')).textContent
    expect(alert).toContain('The server needs attention before setup can finish.')
    expect(alert).toContain('the key was revoked')
    expect(screen.getByRole('dialog', { name: 'Your server is ready' })).toBeTruthy()
  })

  it('is not shown on a finished server, nor while a step is still open', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/repos': { repos: [] }, 'GET /api/templates': TEMPLATES })
    const finished = renderApp(<ReposPage />)
    expect(await screen.findByText('Start your first sandbox')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    finished.unmount()

    stubs(finishes, { setup: state(['address'], { can_complete: false }, READY) })
    renderApp(<ReposPage />)
    expect(await screen.findByText('Start your first sandbox')).toBeTruthy()
    await act(() => new Promise(r => setTimeout(r, 50)))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('the template builds’ line', () => {
  const building = (over: Record<string, unknown>) => ({ blocked: '', runtimes: { boat: [
    { key: 'sbx-base', phase: 'building', status: 'building', template: null, error: null, ...over },
    { key: 'default', phase: 'stale', status: null, template: null, error: null }] } })

  const MIRROR = 'E: Failed to fetch https://cloud.r-project.org/x.bz2 File has unexpected size (68208 != 67689). Mirror sync in progress?'
  const WORDS = 'A package mirror (cloud.r-project.org) was being updated while the template built, so its files didn’t match.'

  it('says when a failed build is tried again, in words, with the log under Details', async () => {
    const at = '2026-11-19T18:40:00Z'
    stubApi({ 'GET /api/templates': building({ phase: 'retrying', status: 'failed', error: MIRROR, reason: WORDS, retry_at: at }) })
    renderApp(<TemplateProgressLine />)
    const line = await screen.findByRole('status')
    const time = new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    expect(line.textContent).toContain(`Template sbx-base failed to build; sbx tries again at ${time}.`)
    expect(line.textContent).toContain(WORDS)
    expect(within(line).getByText('Details')).toBeTruthy()   // the log, folded away
  })

  it('shows a stuck build’s reason first, and the raw error alone when sbx doesn’t know it', async () => {
    stubApi({ 'GET /api/templates': building({ phase: 'stuck', status: 'failed', error: MIRROR, reason: WORDS }) })
    const known = renderApp(<TemplateProgressLine />)
    expect((await screen.findByRole('status')).textContent).toMatch(/^Template sbx-base failed to build\.A package mirror/)
    known.unmount()
    stubApi({ 'GET /api/templates': building({ phase: 'stuck', status: 'failed', error: 'setup exited 1', reason: '' }) })
    renderApp(<TemplateProgressLine />)
    expect((await screen.findByRole('status')).textContent).toBe('Template sbx-base failed to build.setup exited 1')
  })

  it('says the usual length only until the build’s steps show, then the time left once it is known', async () => {
    stubApi({ 'GET /api/templates': building({}) })
    const first = renderApp(<TemplateProgressLine />)
    expect((await screen.findByRole('status')).textContent).toBe('Building templates: 0 of 2 ready · sbx-base, about 10 minutes. You can keep going.')
    first.unmount()

    // A first build has no earlier one to estimate from: on its last step it must not still say ten minutes.
    stubApi({ 'GET /api/templates': building({ step: 22, steps: 22, eta_s: null }) })
    const steps = renderApp(<TemplateProgressLine />)
    expect((await screen.findByRole('status')).textContent).toBe('Building templates: 0 of 2 ready · sbx-base, step 22 of 22. You can keep going.')
    steps.unmount()

    stubApi({ 'GET /api/templates': building({ step: 3, steps: 22, eta_s: 400 }) })
    renderApp(<TemplateProgressLine />)
    expect((await screen.findByRole('status')).textContent).toBe('Building templates: 0 of 2 ready · sbx-base, step 3 of 22, about 7 min left. You can keep going.')
  })
})

describe('the repositories page with none yet', () => {
  it('shows an admin the three steps to a first sandbox', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/repos': { repos: [] }, 'GET /api/templates': TEMPLATES })
    renderApp(<ReposPage />)
    const first = within(await screen.findByRole('region', { name: 'Your first repository' }))
    expect(first.getByText('Start your first sandbox')).toBeTruthy()
    expect(first.getAllByRole('listitem').map(li => li.querySelector('p')?.textContent)).toEqual([
      '1. Connect a repository', '2. Paste the prompt into your coding agent', '3. Open the first sandbox'])
    expect(first.getByRole('link', { name: /Connect repository/ }).getAttribute('href')).toBe('/app/repos/new')
  })

  it('tells someone who is not an admin who connects them', async () => {
    stubApi({ 'GET /api/me': { ...ME, admin: false }, 'GET /api/repos': { repos: [] } })
    renderApp(<ReposPage />)
    expect(await screen.findByText('No repositories yet')).toBeTruthy()
    expect(screen.getByText(/ask one to connect yours/)).toBeTruthy()
  })
})

describe('a certificate that failed', () => {
  const LIMITED = (at: string) => `Saving debug log to /opt/sbx-tls/certbot-logs/letsencrypt.log
Requesting a certificate for sbx.example.com and *.sbx.example.com
An unexpected error occurred:
too many certificates (5) already issued for this exact set of identifiers in the last 168h0m0s, retry after ${at} UTC: see https://letsencrypt.org/docs/rate-limits/#new-certificates-per-exact-set-of-identifiers`

  it('says Let’s Encrypt’s weekly limit in words, and holds Try again until it lifts', async () => {
    const at = new Date(Date.now() + 3600_000)
    const utc = at.toISOString().slice(0, 19).replace('T', ' ')
    admin({ 'GET /api/setup': state([], {}, { ...SAVED, certificate_error: LIMITED(utc) }) })
    renderApp(<SetupPage />)
    const alert = (await screen.findByRole('alert')).textContent ?? ''
    expect(alert).toContain('Let’s Encrypt’s weekly limit is reached')
    expect(alert).toContain('It issued 5 certificates for sbx.example.com this week.')
    expect(alert).not.toContain('certbot')
    const hold = screen.getByRole('button', { name: /^Try again at / }) as HTMLButtonElement
    expect(hold.disabled).toBe(true)
    expect(hold.textContent).toBe(`Try again at ${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`)
  })

  it('offers Try again once the limit has lifted', async () => {
    admin({ 'GET /api/setup': state([], {}, { ...SAVED, certificate_error: LIMITED('2020-01-01 00:00:00') }) })
    renderApp(<SetupPage />)
    expect(await screen.findByText('Let’s Encrypt’s weekly limit is reached')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Try again' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('names a blocked port 53 and where to open it', async () => {
    const { CertificateError } = await import('@/pages/setup/SetupPage')
    const { render } = await import('@testing-library/react')
    const view = render(<CertificateError domain="sbx.example.com" error="port 53: no public resolver (1.1.1.1, 8.8.8.8) reached this server on port 53 within 5 s; fix: allow TCP and UDP port 53 in your provider's firewall" />)
    const text = view.getByRole('alert').textContent ?? ''
    expect(text).toContain('Port 53 is blocked')
    expect(text).toContain('TCP and UDP port 53')
  })
})
