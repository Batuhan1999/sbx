import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Sandbox, SandboxRuntime } from '@/api/types'
import { RepoRuntime } from '@/pages/repos/RepoRuntime'
import { NewSandboxForm } from '@/pages/sandboxes/NewSandboxForm'
import { SandboxTable } from '@/pages/sandboxes/SandboxTable'
import { Runtimes } from '@/pages/settings/SettingsPage'
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
