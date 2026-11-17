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

const section = () => screen.findByRole('region', { name: 'Shared servers' })
/** No port of a target shows anywhere on the page. */
const noPort = () => expect(document.body.textContent).not.toMatch(/:\d{2,5}\b|5432|8080/)

describe('Settings → Shared servers', () => {
  it('is hidden while no runtime needs it and nothing is reachable', async () => {
    const calls = stubApi({ 'GET /api/endpoints': data({ needed: false }) })
    renderApp(<Endpoints />)
    await waitFor(() => expect(calls.some(c => c.url === '/api/endpoints')).toBe(true))
    await new Promise(r => setTimeout(r, 20))
    expect(screen.queryByText('Shared servers')).toBeNull()
  })

  it('lists each endpoint once: address, kind, state, repositories, never a port', async () => {
    stubApi({
      'GET /api/endpoints': data({ needed: false, endpoints: [PG, BILLING, endpoint('temporal', { server_kind: 'temporal' })] }),
      'GET /api/runtimes': { default: 'embed', runtimes: [{ kind: 'e2b-cloud', title: 'E2B Cloud', default: false, host: false, capabilities: { snapshots: false, private_networks: false, egress_rules: false }, limits: {}, status: { state: 'connected', summary: '' } }] },
    })
    renderApp(<Endpoints />)
    const s = await section()
    expect(await within(s).findByText('Let sandboxes at E2B Cloud reach your servers.')).toBeTruthy()
    const list = within(s).getByRole('list')
    const pg = within(list).getByText('dev-pg.example.com').closest('li')!
    expect(within(pg).getByText('Shared')).toBeTruthy()
    expect(s.querySelector('[class*="bg-ok"], [class*="text-ok"], [class*="text-accent"], [class*="bg-accent"]')).toBeNull()
    expect(pg.textContent).toContain('Postgres·acme/shop, acme/api')
    expect(pg.textContent!.match(/acme\/shop/g)).toHaveLength(1)
    expect(within(pg).queryByRole('button', { name: /^Edit/ })).toBeNull()
    expect(within(pg).getByRole('button', { name: 'Copy dev-pg.example.com' })).toBeTruthy()
    expect((within(pg).getByRole('button', { name: 'Stop sharing' }) as HTMLButtonElement).disabled).toBe(true)
    expect(within(pg).getByText('In use')).toBeTruthy()
    const billing = within(list).getByText('billing.example.com').closest('li')!
    expect(billing.textContent).toContain('Internal host·acme/shop')
    expect(within(billing).getByRole('button', { name: 'Edit repositories for billing' })).toBeTruthy()
    expect(within(list).getByText('temporal.example.com').closest('li')!.textContent).toContain('No repositories — attach temporal to one')
    noPort()
  })

  it('without a domain, says one is needed and links to it, with no form', async () => {
    stubApi({ 'GET /api/endpoints': data({ domain: '' }) })
    renderApp(<Endpoints />)
    const s = await section()
    expect(s.textContent).toContain('Set a domain first.')
    expect(within(s).getByRole('link', { name: 'Set domain' }).getAttribute('href')).toBe('/app/settings/general#domain')
    expect(within(s).queryByRole('button', { name: 'Share a server' })).toBeNull()
  })

  it('shares a managed server: the confirm step shows its address, then it applies until shared', async () => {
    const applying = endpoint('dev-pg', { server_kind: 'postgres', state: 'applying' })
    const after = inTurn(ok(data({ endpoints: [applying], candidates: [] })), ok(data({ endpoints: [{ ...applying, state: 'on' }], candidates: [] })))
    let added = false
    const calls = stubApi({
      'POST /api/endpoints': () => { added = true; return { status: 202, body: applying } },
      'GET /api/endpoints': () => (added ? after() : ok(data({ candidates: [{ server: 'dev-pg', kind: 'postgres', target: '172.29.50.10:5432', address: 'dev-pg.db.example.com' }] }))),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within(await section()).getByRole('button', { name: 'Share a server' }))
    expect((screen.getByRole('radio', { name: /dev-pg/ }) as HTMLInputElement).checked).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    const confirm = screen.getByRole('region', { name: 'Share dev-pg?' })
    expect(within(confirm).getByTestId('endpoint-address').textContent).toBe('dev-pg.db.example.com')
    expect(confirm.textContent).toContain('Reachable over the internet, only by sandboxes of repositories dev-pg is attached to.')
    expect(confirm.textContent).toContain("Each sandbox has its own certificate, and the service's login still applies.")
    noPort()
    await user.click(within(confirm).getByRole('button', { name: 'Share' }))
    expect(calls.find(c => c.method === 'POST')).toMatchObject({ url: '/api/endpoints', body: { server: 'dev-pg' } })
    const row = (await screen.findByText('dev-pg.example.com')).closest('li')!
    expect(within(row).getByRole('status').textContent).toContain('Sharing…')
    await waitFor(() => expect(within(row).getByText('Shared')).toBeTruthy(), { timeout: 4000 })
    expect(calls.filter(c => c.method === 'GET').length).toBeGreaterThanOrEqual(3)
  }, 8000)

  it('shares an internal host with chosen repositories, checking its name and IP address and port first', async () => {
    const calls = stubApi({
      'POST /api/endpoints': (body: unknown) => ({ status: 202, body: endpoint('billing', { kind: 'internal', server: '', state: 'applying', ...(body as object) }) }),
      'GET /api/endpoints': data({ endpoints: [PG], candidates: [] }),
      'GET /api/repos': REPOS,
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    await user.click(within(await section()).getByRole('button', { name: 'Share a server' }))
    expect((screen.getByRole('radio', { name: /Internal host/ }) as HTMLInputElement).checked).toBe(true)
    const picker = screen.getByRole('group', { name: 'Repositories' })
    expect(await within(picker).findByRole('checkbox', { name: 'acme/docs' })).toBeTruthy()
    await user.type(screen.getByLabelText('Name'), 'Billing_API')
    await user.type(screen.getByLabelText('IP address and port'), 'billing.internal:8080') // a host name: refused, as the server does
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText('Use lowercase letters, digits and hyphens.')).toBeTruthy()
    expect(screen.getByText('Enter an IP address and port, such as 10.0.0.5:7233.')).toBeTruthy()
    expect(screen.getByText('Choose at least one repository.')).toBeTruthy()
    expect(calls.some(c => c.method === 'POST')).toBe(false)
    await new Promise(r => requestAnimationFrame(r)) // the form focuses the first invalid field
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'dev-pg')
    expect(screen.getByText('That name is taken.')).toBeTruthy()
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'billing')
    expect(screen.getByText('billing.example.com')).toBeTruthy()
    await user.clear(screen.getByLabelText('IP address and port'))
    await user.type(screen.getByLabelText('IP address and port'), '10.0.0.40:8080')
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/shop' }))
    await user.click(within(picker).getByRole('checkbox', { name: 'acme/api' }))
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    const confirm = screen.getByRole('region', { name: 'Share billing?' })
    expect(within(confirm).getByTestId('endpoint-address').textContent).toBe('billing.example.com')
    expect(confirm.textContent).toContain('Reachable over the internet, only by sandboxes of acme/shop and acme/api.')
    noPort()
    // Back keeps what was entered.
    await user.click(within(confirm).getByRole('button', { name: 'Back' }))
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('billing')
    expect((screen.getByRole('checkbox', { name: 'acme/api' }) as HTMLInputElement).checked).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(within(screen.getByRole('region', { name: 'Share billing?' })).getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')!.body).toEqual({ name: 'billing', target: '10.0.0.40:8080', repos: ['acme/shop', 'acme/api'] })
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
    await user.click(within(row).getByRole('button', { name: 'Edit repositories for billing' }))
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
    await user.click(within(await section()).getByRole('button', { name: 'Share a server' }))
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: 'Share' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Connect a domain first.')
    expect(screen.getByRole('button', { name: 'Back' })).toBeTruthy()
  })

  it('shows a failed endpoint with why', async () => {
    stubApi({ 'GET /api/endpoints': data({ endpoints: [endpoint('dev-ch', { server_kind: 'clickhouse', state: 'failed', message: 'The certificate could not be issued' })] }) })
    renderApp(<Endpoints />)
    const row = (await screen.findByText('dev-ch.example.com')).closest('li')!
    // Said once, in the message: no badge beside it, and nothing says "Shared".
    expect(within(row).getByRole('note').textContent).toBe('Sharing failed: The certificate could not be issued')
    expect(row.textContent!.match(/failed/gi)).toHaveLength(1)
    expect(within(row).queryByText('Shared')).toBeNull()
    expect(row.textContent).toContain('ClickHouse')
  })

  it('stops sharing after a confirm', async () => {
    let removed = false
    const calls = stubApi({
      'DELETE /api/endpoints/temporal': () => { removed = true; return { status: 202, body: null } },
      'GET /api/endpoints': () => ok(data({ endpoints: removed ? [] : [TEMPORAL] })),
    })
    renderApp(<Endpoints />)
    const user = userEvent.setup()
    const row = (await screen.findByText('temporal.example.com')).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Stop sharing' }))
    const dialog = screen.getByRole('dialog', { name: 'Stop sharing temporal?' })
    expect(dialog.textContent).toContain('Sandboxes at the provider lose access to it.')
    await user.click(within(dialog).getByRole('button', { name: 'Stop sharing' }))
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
    await user.click(within((await screen.findByText('temporal.example.com')).closest('li')!).getByRole('button', { name: 'Stop sharing' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Stop sharing' }))
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

  it('names the servers it reaches, linking to the section', async () => {
    stub(data({ endpoints: [PG, TEMPORAL, endpoint('dev-ch', { state: 'applying' })] }))
    renderApp(<Runtimes />)
    const cloud = (await screen.findByText('E2B Cloud')).closest('li')!
    const link = await within(cloud).findByRole('link')
    expect(cloud.textContent).toContain('Reaches dev-pg, temporal')
    expect(link.getAttribute('href')).toBe('/app/settings/general#endpoints')
    expect(screen.getByText('E2B Embed').closest('li')!.textContent).not.toContain('Reaches')
  })

  it('says when it reaches none', async () => {
    stub(data())
    renderApp(<Runtimes />)
    const cloud = (await screen.findByText('E2B Cloud')).closest('li')!
    expect((await within(cloud).findByRole('link', { name: 'Share a server' })).getAttribute('href')).toBe('/app/settings/general#endpoints')
    expect(cloud.textContent).toContain('Reaches nothing yet — Share a server')
  })
})
