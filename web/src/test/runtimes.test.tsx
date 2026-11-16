import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setSetupToken } from '@/api/client'
import type { Sandbox, SandboxRuntime } from '@/api/types'
import { Outlet, Route, Routes } from 'react-router'
import type { RepoContext } from '@/pages/repos/RepoLayout'
import { RepoRuntime } from '@/pages/repos/RepoRuntime'
import RepoSetup from '@/pages/repos/RepoSetup'
import SetupPage from '@/pages/setup/SetupPage'
import { NewSandboxForm } from '@/pages/sandboxes/NewSandboxForm'
import { SandboxTable } from '@/pages/sandboxes/SandboxTable'
import { Runtimes } from '@/pages/settings/Runtimes'
import { ME, renderApp, stubApi } from './render'

const runtime = (kind: string, title: string, extra: Partial<SandboxRuntime> = {}): SandboxRuntime => ({
  kind, title, default: false, host: false, capabilities: {}, limits: {}, status: { state: 'connected', summary: '' }, ...extra,
})
const EMBED = runtime('embed', 'E2B Embed', { default: true, host: true, limits: { cpu: 16, memory_mb: 30720 }, status: { state: 'connected', summary: '3 sandboxes running' } })
const HOSTED = runtime('hosted', 'Hosted runtime', { limits: { sandboxes: 20 }, status: { state: 'attention', summary: 'The API key was refused' } })
const ONE = { default: 'embed', runtimes: [EMBED] }
const TWO = { default: 'embed', runtimes: [EMBED, HOSTED] }
const REPO = { repo: 'usememos/memos', trust: 'trusted', configured: true, prebuilt: false, previews: [], secrets_missing: [] }

const sandbox = (name: string, runtime: string): Sandbox => ({
  name, owner: 'ada@example.com', branch: 'main', sandbox_id: 'id', status: 'up', error: null, urls: {}, app: 'usememos/memos', vm: 'running',
  created_at: null, origin: { kind: 'person', label: 'Started here' }, actions: { pause: true, resume: false, restart: true, remove: true, logs: true }, runtime,
})

describe('sandbox runtimes in settings', () => {
  it('lists each enabled runtime: default, where it runs, its status and its limits', async () => {
    stubApi({ 'GET /api/runtimes': TWO })
    renderApp(<Runtimes />)
    const embed = (await screen.findByText('E2B Embed')).closest('li')!
    expect(within(embed).getByText('Default')).toBeTruthy()
    expect(within(embed).getByText('Connected')).toBeTruthy()
    expect(embed.textContent).toContain('Runs on this server. 3 sandboxes running')
    expect(embed.textContent).toContain('Each at most 16 vCPUs, 30720 MB memory')
    const hosted = screen.getByText('Hosted runtime').closest('li')!
    expect(within(hosted).queryByText('Default')).toBeNull()
    expect(within(hosted).getByText('Needs attention')).toBeTruthy()
    expect(hosted.textContent).toContain('Runs at the provider. The API key was refused')
    expect(hosted.textContent).toContain('Up to 20 running at once')
  })
})

describe('with one runtime', () => {
  it('shows no runtime on sandboxes, in the new sandbox form or on the repo', async () => {
    const calls = stubApi({ 'GET /api/me': ME, 'GET /api/runtimes': ONE, 'GET /api/repos': { repos: [] } })
    renderApp(<>
      <SandboxTable sandboxes={[sandbox('login', 'embed')]} />
      <NewSandboxForm repo="usememos/memos" />
      <RepoRuntime repo="usememos/memos" />
    </>)
    await waitFor(() => expect(calls.some(c => c.url === '/api/runtimes')).toBe(true))
    await screen.findByLabelText('Name')
    expect(screen.queryByText('E2B Embed')).toBeNull()
    expect(screen.queryByLabelText('Runtime')).toBeNull()
    expect(calls.some(c => c.url === '/api/repos/usememos/memos')).toBe(false)
  })
})

describe('with several runtimes', () => {
  it('labels each sandbox with its runtime', async () => {
    stubApi({ 'GET /api/runtimes': TWO })
    renderApp(<SandboxTable sandboxes={[sandbox('login', 'embed'), sandbox('search', 'hosted')]} />)
    const rows = await screen.findAllByRole('row')
    expect(await within(rows[1]).findByText('E2B Embed')).toBeTruthy()
    expect(within(rows[2]).getByText('Hosted runtime')).toBeTruthy()
  })

  it('offers the runtime when creating, defaulting to the repo’s choice', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/runtimes': TWO, 'GET /api/repos/usememos/memos': { ...REPO, runtime: 'hosted' }, 'GET /api/repos': { repos: [] },
      'POST /api/sandboxes': { name: 'login' },
    })
    renderApp(<NewSandboxForm repo="usememos/memos" />)
    const user = userEvent.setup()
    const select = await screen.findByLabelText('Runtime') as HTMLSelectElement
    expect(select.value).toBe('')
    expect(await screen.findByRole('option', { name: 'Repository’s choice (Hosted runtime)' })).toBeTruthy()
    await user.type(screen.getByLabelText(/Branch/), 'login')
    await user.click(screen.getByRole('button', { name: 'Create sandbox' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')!.body).not.toHaveProperty('runtime')
  })

  it('sends the runtime picked, and shows a refusal’s reason', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/runtimes': TWO, 'GET /api/repos/usememos/memos': { ...REPO, runtime: null }, 'GET /api/repos': { repos: [] },
      'POST /api/sandboxes': () => ({ status: 400, body: { detail: 'Hosted runtime cannot run usememos/memos: its managed servers need private networks.' } }),
    })
    renderApp(<NewSandboxForm repo="usememos/memos" />)
    const user = userEvent.setup()
    expect(await screen.findByRole('option', { name: 'Installation default (E2B Embed)' })).toBeTruthy()
    await user.selectOptions(screen.getByLabelText('Runtime'), 'hosted')
    await user.type(screen.getByLabelText(/Branch/), 'login')
    await user.click(screen.getByRole('button', { name: 'Create sandbox' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Hosted runtime cannot run usememos/memos')
    expect(calls.find(c => c.method === 'POST')!.body).toMatchObject({ runtime: 'hosted' })
  })

  it('lets an admin set the repo’s runtime, and go back to the default', async () => {
    const calls = stubApi({
      'GET /api/runtimes': TWO, 'GET /api/repos/usememos/memos': { ...REPO, runtime: 'hosted' },
      'PUT /api/repos/usememos/memos/runtime': (body: unknown) => ({ status: 200, body: { ...REPO, ...(body as object) } }),
    })
    renderApp(<RepoRuntime repo="usememos/memos" />)
    const user = userEvent.setup()
    await waitFor(() => expect((screen.getByLabelText('Runtime') as HTMLSelectElement).value).toBe('hosted'))
    await user.selectOptions(screen.getByLabelText('Runtime'), '')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
    expect(calls.find(c => c.method === 'PUT')).toMatchObject({ url: '/api/repos/usememos/memos/runtime', body: { runtime: null } })
  })
})

const SETUP = {
  complete: false, steps: [{ step: 'address', done: true, optional: false, summary: '' }, { step: 'sign_in', done: false, optional: false, summary: '' }],
  signed_in: false, email: null, can_complete: false, host: { address: '203.0.113.7' },
  access: { mode: 'ip', domain: '', url: 'https://203.0.113.7', certificate: '', certificate_email: '', certificate_ready: true, certificate_error: '', needs_ports: [] },
  link: 'http://localhost:3000/app/setup', admins: [], version: 'dev',
}
const CHECKS = { ok: true, checks: [{ name: 'Virtualization', ok: true, detail: 'KVM is available', fix: '' }] }

describe('first-run setup', () => {
  it('shows each enabled runtime next to the server checks', async () => {
    stubApi({ 'GET /api/setup/checks': CHECKS, 'GET /api/setup': SETUP, 'GET /api/runtimes': TWO })
    renderApp(<SetupPage />, '/app/setup')
    expect(await screen.findByText('KVM is available')).toBeTruthy()
    const list = await screen.findByRole('list', { name: 'Sandbox runtimes' })
    expect(within(list).getByText('E2B Embed').closest('li')!.textContent).toContain('Runs on this server')
    expect(within(list).getByText('Hosted runtime').closest('li')!.textContent).toContain('Runs at the provider')
  })

  it('keeps the server checks alone when the runtimes cannot be read', async () => {
    setSetupToken('one-time-token')  // the setup link's token: GET /api/runtimes takes a sign-in, so it answers 401
    const calls = stubApi({ 'GET /api/setup/checks': CHECKS, 'GET /api/setup': SETUP, 'GET /api/runtimes': () => ({ status: 401, body: { detail: 'sign in' } }) })
    renderApp(<SetupPage />, '/app/setup')
    expect(await screen.findByText('KVM is available')).toBeTruthy()
    await waitFor(() => expect(calls.some(c => c.url === '/api/runtimes')).toBe(true))
    expect(screen.queryByText('Sandbox runtimes')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('repository facts', () => {
  const readiness = { repo: 'usememos/memos', info: { ...REPO, github: 'app', previews: ['web'] }, steps: [], stage: 'ready', done: 0, total: 1, next: null }
  const page = () => <Outlet context={{ repo: 'usememos/memos', readiness, env: undefined, envError: null, me: ME, trusted: true } as unknown as RepoContext} />

  it('names where the repository’s sandboxes run when there is a choice', async () => {
    stubApi({ 'GET /api/runtimes': TWO, 'GET /api/repos/usememos/memos/setup-pr': { available: false, pull_request: null }, 'GET /api/repos/usememos/memos': { ...REPO, runtime: null } })
    renderApp(<RepoRuntimeFacts layout={page} />)
    expect(await screen.findByText('Installation default (E2B Embed)')).toBeTruthy()
  })

  it('says nothing about runtimes with one', async () => {
    const calls = stubApi({ 'GET /api/runtimes': ONE, 'GET /api/repos/usememos/memos/setup-pr': { available: false, pull_request: null } })
    renderApp(<RepoRuntimeFacts layout={page} />)
    await waitFor(() => expect(calls.some(c => c.url === '/api/runtimes')).toBe(true))
    expect(screen.getByText('Previews')).toBeTruthy()
    expect(screen.queryByText('Runtime')).toBeNull()
  })
})

function RepoRuntimeFacts({ layout }: { layout: () => React.ReactNode }) {
  return <Routes><Route element={layout()}><Route path="*" element={<RepoSetup />} /></Route></Routes>
}
