import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Endpoint, Endpoints as EndpointsData, SandboxRuntime } from '@/api/types'
import { Endpoints } from '@/pages/settings/Endpoints'
import { Runtimes } from '@/pages/settings/Runtimes'
import { ME, renderApp, stubApi } from './render'

const endpoint = (name: string, extra: Partial<Endpoint> = {}): Endpoint => ({
  name, address: `${name}.example.com`, kind: 'managed-server', server: name, target: '172.29.50.10:5432', state: 'on', message: '', used_by: [], repos: [], ...extra,
})
const PG = endpoint('dev-pg', { server_kind: 'postgres', used_by: ['acme/shop'], repos: ['acme/shop', 'acme/api'] })
const BILLING = endpoint('billing', { kind: 'internal', server: '', target: '10.0.0.5:8080', repos: ['acme/shop'] })
const repo = (name: string) => ({ repo: name, trust: 'trusted', configured: true, prebuilt: false, built_at: null, sandboxes: 0, state: { label: '', level: 'ok' } })
const REPOS = { repos: [repo('acme/shop'), repo('acme/api'), repo('acme/docs')] }
const TEMPORAL = endpoint('temporal', { server_kind: 'temporal' })
const data = (extra: Partial<EndpointsData> = {}): EndpointsData => ({
  endpoints: [], candidates: [{ server: 'dev-pg', kind: 'postgres', target: '172.29.50.10:5432' }], domain: 'example.com', needed: true, ...extra,
})
const ok = (body: unknown) => ({ status: 200, body })

/** Answers in turn, the last one again and again. */
function inTurn(...answers: { status: number; body: unknown }[]) {
  let i = 0
  return () => answers[Math.min(i++, answers.length - 1)]
}

const section = () => screen.findByRole('region', { name: 'Reachable from provider sandboxes' })
/** No port of a target shows anywhere on the page. */
const noPort = () => expect(document.body.textContent).not.toMatch(/:\d{2,5}\b|5432|8080/)

describe('Settings → Reachable from provider sandboxes', () => {
  it('is hidden while no runtime needs it and nothing is reachable', async () => {
    const calls = stubApi({ 'GET /api/endpoints': data({ needed: false }) })
    renderApp(<Endpoints />)
    await waitFor(() => expect(calls.some(c => c.url === '/api/endpoints')).toBe(true))
    await new Promise(r => setTimeout(r, 20))
    expect(screen.queryByText('Reachable from provider sandboxes')).toBeNull()
  })

  it('lists each endpoint: its address, what it is, its state and who uses it, never a port', async () => {
    stubApi({ 'GET /api/endpoints': data({ needed: false, endpoints: [PG, BILLING] }) })
    renderApp(<Endpoints />)
    const list = within(await section()).getByRole('list')
    const pg = within(list).getByText('dev-pg.example.com').closest('li')!
    expect(pg.textContent).toContain('dev-pg · Postgres')
    expect(within(pg).getByText('Reachable')).toBeTruthy()
    expect(pg.textContent).toContain('Reached by: acme/shop, acme/api')
    expect(pg.textContent).toContain('attaching it to a repository grants')
    expect(within(pg).queryByRole('button', { name: /^Edit/ })).toBeNull()
    expect(within(pg).getByRole('button', { name: 'Copy dev-pg.example.com' })).toBeTruthy()
    expect((within(pg).getByRole('button', { name: 'Stop exposing' }) as HTMLButtonElement).disabled).toBe(true)
    expect(pg.textContent).toContain("Can't stop exposing it while acme/shop uses it.")
    const billing = within(list).getByText('billing.example.com').closest('li')!
    expect(billing.textContent).toContain('Internal host')
    expect(billing.textContent).toContain('Repositories: acme/shop')
    expect(within(billing).getByRole('button', { name: 'Edit repositories for billing.example.com' })).toBeTruthy()
    noPort()
  })

  it('without a domain, says one is needed and links to it, with no form', async () => {
    stubApi({ 'GET /api/endpoints': data({ domain: '' }) })
    renderApp(<Endpoints />)
    const s = await section()
    expect(s.textContent).toContain('A domain is needed first')
    expect(within(s).getByRole('link', { name: 'Set the domain' }).getAttribute('href')).toBe('/app/settings/general#domain')
    expect(within(s).queryByRole('button', { name: 'Make reachable' })).toBeNull()
  })

  it('makes a managed server reachable: the confirm step shows its address, then it applies until on', async () => {
    const applying = endpoint('dev-pg', { server_kind: 'postgres', state: 'applying' })
    const after = inTurn(ok(data({ endpoints: [applying], candidates: [] })), ok(data({ endpoints: [{ ...applying, state: 'on' }], candidates: [] })))
    let added = false
    const calls = stubApi({
      'POST /api/endpoints': () => { added = true; return { status: 202, body: applying } },
      'GET /api/endpoints': () => (added ? after() : ok(data({ candidates: [{ server: 'dev-pg', kind: 'postgres', target: '172.29.50.10:5432', address: 'dev-pg.db.example.com' }] }))),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within(await section()).getByRole('button', { name: 'Make reachable' }))
    expect((screen.getByRole('radio', { name: /dev-pg/ }) as HTMLInputElement).checked).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    const confirm = screen.getByRole('region', { name: 'Make dev-pg reachable?' })
    expect(within(confirm).getByTestId('endpoint-address').textContent).toBe('dev-pg.db.example.com')
    expect(confirm.textContent).toContain('Only sandboxes of repositories dev-pg is attached to reach it.')
    expect(confirm.textContent).toContain('reachable from the internet')
    expect(confirm.textContent).toContain('only by this installation')
    expect(confirm.textContent).toContain('own certificate')
    expect(confirm.textContent).toContain('login still applies')
    noPort()
    await user.click(within(confirm).getByRole('button', { name: 'Make reachable' }))
    expect(calls.find(c => c.method === 'POST')).toMatchObject({ url: '/api/endpoints', body: { server: 'dev-pg' } })
    const row = (await screen.findByText('dev-pg.example.com')).closest('li')!
    expect(within(row).getByRole('status').textContent).toContain('Making reachable')
    await waitFor(() => expect(within(row).getByText('Reachable')).toBeTruthy(), { timeout: 4000 })
    expect(calls.filter(c => c.method === 'GET').length).toBeGreaterThanOrEqual(3)
  }, 8000)

  it('makes an internal host reachable, checking its name and host:port first', async () => {
    const calls = stubApi({
      'POST /api/endpoints': (body: unknown) => ({ status: 202, body: endpoint('billing', { kind: 'internal', server: '', state: 'applying', ...(body as object) }) }),
      'GET /api/endpoints': data({ endpoints: [PG], candidates: [] }),
      'GET /api/repos': REPOS,
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within(await section()).getByRole('button', { name: 'Make reachable' }))
    expect((screen.getByRole('radio', { name: /An internal host/ }) as HTMLInputElement).checked).toBe(true)
    const picker = screen.getByRole('group', { name: 'Repositories' })
    expect(await within(picker).findByRole('checkbox', { name: 'acme/docs' })).toBeTruthy()
    await user.type(screen.getByLabelText('Name'), 'Billing_API')
    await user.type(screen.getByLabelText('Host and port'), 'billing.internal')
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText(/Use lowercase letters, digits and hyphens/)).toBeTruthy()
    expect(screen.getByText('Enter the host and its port as host:port.')).toBeTruthy()
    expect(screen.getByText('Choose at least one repository.')).toBeTruthy()
    expect(calls.some(c => c.method === 'POST')).toBe(false)
    await new Promise(r => requestAnimationFrame(r)) // the form focuses the first invalid field
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'dev-pg')
    expect(screen.getByText('Something is already reachable under that name.')).toBeTruthy()
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'billing')
    expect(screen.getByText('billing.example.com')).toBeTruthy()
    await user.type(screen.getByLabelText('Host and port'), ':8080')
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/shop' }))
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/api' }))
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    const confirm = screen.getByRole('region', { name: 'Make this internal host reachable?' })
    expect(within(confirm).getByTestId('endpoint-address').textContent).toBe('billing.example.com')
    expect(confirm.textContent).toContain('Only sandboxes of acme/shop and acme/api reach it.')
    noPort()
    // Back keeps what was entered.
    await user.click(within(confirm).getByRole('button', { name: 'Back' }))
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('billing')
    expect((screen.getByRole('checkbox', { name: 'acme/api' }) as HTMLInputElement).checked).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(within(screen.getByRole('region', { name: 'Make this internal host reachable?' })).getByRole('button', { name: 'Make reachable' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')!.body).toEqual({ name: 'billing', target: 'billing.internal:8080', repos: ['acme/shop', 'acme/api'] })
  })

  it('edits an internal host’s repositories, and shows a refusal', async () => {
    let answer = { status: 200, body: {} as unknown }
    const calls = stubApi({
      'PUT /api/endpoints/billing': () => answer,
      'GET /api/endpoints': data({ endpoints: [BILLING] }),
      'GET /api/repos': REPOS,
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    const row = (await screen.findByText('billing.example.com')).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Edit repositories for billing.example.com' }))
    const picker = within(row).getByRole('group', { name: 'Repositories' })
    expect((await within(picker).findByRole('checkbox', { name: 'acme/shop' }) as HTMLInputElement).checked).toBe(true)
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/shop' }))
    await user.click(within(row).getByRole('button', { name: 'Save' }))
    expect(within(row).getByText('Choose at least one repository.')).toBeTruthy()
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    answer = { status: 400, body: { detail: 'acme/docs is not connected to this installation' } }
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/docs' }))
    await user.click(within(row).getByRole('button', { name: 'Save' }))
    expect((await within(row).findByRole('alert')).textContent).toContain('acme/docs is not connected')
    answer = { status: 200, body: { ...BILLING, repos: ['acme/docs', 'acme/api'] } }
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/api' }))
    await user.click(within(row).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(row).queryByRole('group', { name: 'Repositories' })).toBeNull())
    expect(calls.filter(c => c.method === 'PUT').at(-1)).toMatchObject({ url: '/api/endpoints/billing', body: { repos: ['acme/docs', 'acme/api'] } })
  })

  it('shows a refusal in the confirm step', async () => {
    stubApi({
      'POST /api/endpoints': () => ({ status: 409, body: { detail: 'Connect a domain first.' } }),
      'GET /api/endpoints': data(),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within(await section()).getByRole('button', { name: 'Make reachable' }))
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: 'Make reachable' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Connect a domain first.')
    expect(screen.getByRole('button', { name: 'Back' })).toBeTruthy()
  })

  it('shows a failed endpoint with why', async () => {
    stubApi({ 'GET /api/endpoints': data({ endpoints: [endpoint('dev-ch', { server_kind: 'clickhouse', state: 'failed', message: 'The certificate could not be issued' })] }) })
    renderApp(<Endpoints />)
    const row = (await screen.findByText('dev-ch.example.com')).closest('li')!
    expect(within(row).getByText('Failed')).toBeTruthy()
    expect(within(row).getByRole('note').textContent).toBe('The certificate could not be issued')
    expect(row.textContent).toContain('dev-ch · ClickHouse')
  })

  it('stops exposing after a confirm', async () => {
    let removed = false
    const calls = stubApi({
      'DELETE /api/endpoints/temporal': () => { removed = true; return { status: 202, body: null } },
      'GET /api/endpoints': () => ok(data({ endpoints: removed ? [] : [TEMPORAL] })),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    const row = (await screen.findByText('temporal.example.com')).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Stop exposing' }))
    const dialog = screen.getByRole('dialog', { name: 'Stop exposing temporal.example.com?' })
    await user.click(within(dialog).getByRole('button', { name: 'Stop exposing' }))
    await waitFor(() => expect(screen.queryByText('temporal.example.com')).toBeNull())
    expect(calls.some(c => c.method === 'DELETE' && c.url === '/api/endpoints/temporal')).toBe(true)
  })

  it('shows the repositories that still use it when the server refuses', async () => {
    stubApi({
      'DELETE /api/endpoints/temporal': () => ({ status: 409, body: { detail: 'temporal is used by acme/shop, acme/api' } }),
      'GET /api/endpoints': data({ endpoints: [TEMPORAL] }),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within((await screen.findByText('temporal.example.com')).closest('li')!).getByRole('button', { name: 'Stop exposing' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Stop exposing' }))
    expect((await within(dialog).findByRole('alert')).textContent).toContain('temporal is used by acme/shop, acme/api')
  })
})

describe('Settings → Sandbox runtimes: what a provider runtime reaches', () => {
  const runtime = (kind: string, title: string, privateNetworks: boolean, extra: Partial<SandboxRuntime> = {}): SandboxRuntime => ({
    kind, title, default: false, host: false, capabilities: { snapshots: false, private_networks: privateNetworks, egress_rules: false }, limits: {}, status: { state: 'connected', summary: '' }, ...extra,
  })
  const RUNTIMES = { default: 'embed', runtimes: [runtime('embed', 'E2B Embed', true, { default: true, host: true }), runtime('e2b-cloud', 'E2B Cloud', false)] }
  const stub = (endpoints: EndpointsData) => stubApi({
    'GET /api/me': ME, 'GET /api/runtimes/available': [], 'GET /api/runtimes/change': { state: 'idle', operation: '', kind: '', message: '', at: '' },
    'GET /api/runtimes': RUNTIMES, 'GET /api/endpoints': endpoints,
  })

  it('names the addresses it reaches, linking to the section', async () => {
    stub(data({ endpoints: [PG, TEMPORAL, endpoint('dev-ch', { state: 'applying' })] }))
    renderApp(<Runtimes />)
    const cloud = (await screen.findByText('E2B Cloud')).closest('li')!
    const link = await within(cloud).findByRole('link')
    expect(cloud.textContent).toContain('Reaches: dev-pg.example.com, temporal.example.com')
    expect(link.getAttribute('href')).toBe('/app/settings/general#endpoints')
    expect(screen.getByText('E2B Embed').closest('li')!.textContent).not.toContain('Reaches')
  })

  it('says when it reaches none', async () => {
    stub(data())
    renderApp(<Runtimes />)
    const cloud = (await screen.findByText('E2B Cloud')).closest('li')!
    expect((await within(cloud).findByRole('link', { name: 'make one reachable' })).getAttribute('href')).toBe('/app/settings/general#endpoints')
    expect(cloud.textContent).toContain('Reaches no shared servers — make one reachable')
  })
})
