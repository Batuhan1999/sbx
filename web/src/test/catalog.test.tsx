import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Connection } from '@/api/types'
import ExtensionsPage from '@/pages/extensions/ExtensionsPage'
import ServicesPage from '@/pages/services/ServicesPage'
import { webAddresses } from '@/components/LinkedText'
import { AttachForm } from '@/components/AttachForm'
import { inputLabel, sentence, summary, suggestKey } from '@/lib/packages'
import { choose, isChecked, ME, optionNames, renderApp, stubApi } from './render'

const LOGO = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4='
const pkg = (name: string, title: string, kind: 'connection' | 'provision', logo = '') => ({
  name, title, kind, logo, description: `${title} for sandboxes`, source: 'shipped', inputs: {}, outputs: {}, env: {},
  requires: {}, runs: {}, checks: false, docs: '', used_by: [],
})
const SERVER = { ...pkg('postgres-server', 'Postgres server', 'connection', LOGO), inputs: { url: { required: true, secret: true, agent: true, help: 'an admin URL', label: 'URL' } } }
const CATALOG = { packages: [SERVER, pkg('static', 'Static values', 'provision')], problems: [] }

const ACCOUNT = {
  ...pkg('openrouter-account', 'OpenRouter account', 'connection', LOGO), description: 'An OpenRouter account. It stays here.', docs: 'https://example.com/keys',
  inputs: {
    management_key: { required: true, secret: true, agent: true, help: 'a management key' },
    region: { required: false, secret: false, agent: true, help: '', default: 'eu', label: 'Data region', choices: [{ value: 'eu', label: 'Europe' }, { value: 'us', label: 'United States' }] },
    verify: { required: false, secret: false, agent: true, help: 'check the key first', default: 'true', type: 'boolean' },
  },
}
const KEY = { ...pkg('openrouter-key', 'OpenRouter API key', 'provision'), description: 'An API key per sandbox. Deleted with it.', requires: { account: { kind: 'openrouter-account' } }, used_by: ['acme/app'] }
const SAVED = { key: 'openrouter', package: 'openrouter-account', title: 'OpenRouter account', logo: LOGO, inputs: {}, used_by: [], known: true, check_status: 'passed' }
const base = (extra: Record<string, unknown> = {}) => ({
  'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
  'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] }, ...extra,
})
const group = async (name: string) => within((await screen.findByRole('heading', { name })).closest('section')!)
const menu = async (user: ReturnType<typeof userEvent.setup>, label: string, item: string) => {
  await user.click(await screen.findByRole('button', { name: label }))
  await user.click(await screen.findByRole('menuitem', { name: item }))
}

describe('the Add group', () => {
  it('offers each connection package not connected yet as a tile with its logo, and no per-sandbox packages', async () => {
    stubApi(base({ 'GET /api/catalog': CATALOG }))
    renderApp(<ServicesPage />)
    const add = await group('Add')
    const tile = (await add.findByText('Postgres server')).closest('li')!
    expect(tile.querySelector('img')?.getAttribute('src')).toBe(LOGO)
    expect(within(tile).queryByText('Postgres server for sandboxes')).toBeNull()   // the description sits in its dialog
    expect(add.queryByText('Static values')).toBeNull()
    expect(screen.queryByText(/Not connected/)).toBeNull()
  })

  it('connects a package from its dialog, which holds its description, and closes when saved', async () => {
    const calls = stubApi(base({ 'GET /api/catalog': CATALOG, 'POST /api/catalog/connections': { key: 'postgres' } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add Postgres server' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Postgres server for sandboxes')).toBeTruthy()
    await user.type(await within(dialog).findByLabelText('URL'), 'postgresql://admin@fixture/db')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({ package: 'postgres-server', key: 'postgres', inputs: { url: 'postgresql://admin@fixture/db' } })
  })

  it('asks for inputs by label, offers choices as a list and yes or no as a switch, the name under More options', async () => {
    const calls = stubApi(base({ 'POST /api/catalog/connections': { key: 'openrouter' } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add OpenRouter account' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(await within(dialog).findByLabelText('Management key'), 'fixture-key')
    expect(within(dialog).getByText('A management key.')).toBeTruthy()
    const region = within(dialog).getByLabelText('Data region')
    expect(await optionNames(user, region)).toEqual(['Europe', 'United States'])
    await choose(user, region, 'United States')
    const verify = within(dialog).getByRole('switch', { name: /Verify/ })
    expect(isChecked(verify)).toBe(true)
    await user.click(verify)
    expect(within(dialog).queryByLabelText('Name')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'More options' }))
    expect((within(dialog).getByLabelText('Name') as HTMLInputElement).value).toBe('openrouter')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      package: 'openrouter-account', key: 'openrouter', inputs: { management_key: 'fixture-key', region: 'us', verify: 'false' },
    }))
  })

  it('checks a number input before sending it', async () => {
    const calls = stubApi(base({ 'GET /api/catalog': { packages: [{ ...SERVER, inputs: { budget: { required: true, secret: false, agent: true, help: '', type: 'integer', min: 20, label: 'Budget' } } }], problems: [] } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add Postgres server' }))
    const dialog = await screen.findByRole('dialog')
    const budget = await within(dialog).findByLabelText('Budget') as HTMLInputElement
    expect(budget.type).toBe('number')
    expect(budget.min).toBe('20')
    await user.type(budget, '5')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    expect(await within(dialog).findByText('Enter at least 20.')).toBeTruthy()
    expect(calls.some(c => c.method === 'POST')).toBe(false)
  })

  it('shows its address as a link in a field\'s help', async () => {
    stubApi(base({ 'GET /api/catalog': { packages: [{ ...ACCOUNT, inputs: { management_key: { required: true, secret: true, agent: true, help: 'a key from openrouter.ai/settings/keys' } } }, KEY], problems: [] } }))
    renderApp(<ServicesPage />)
    await userEvent.click(await screen.findByRole('button', { name: 'Add OpenRouter account' }))
    const dialog = await screen.findByRole('dialog')
    expect((await within(dialog).findByRole('link', { name: 'openrouter.ai/settings/keys' })).getAttribute('href')).toBe('https://openrouter.ai/settings/keys')
  })
})

describe('the Connected group', () => {
  it('lists a connected package there, not under Add, quiet while it works', async () => {
    stubApi(base({ 'GET /api/catalog/connections': { connections: [{ ...SAVED, used_by: ['acme/app:openrouter'] }] } }))
    renderApp(<ServicesPage />)
    const connected = await group('Connected')
    const row = (await connected.findByText('OpenRouter account')).closest('li')!
    expect(within(row).getByRole('link', { name: 'acme/app' }).getAttribute('href')).toBe('/app/repos/acme/app/packages')
    expect(within(row).queryByText('Connected')).toBeNull()
    expect((await group('Add')).queryByText('OpenRouter account')).toBeNull()
  })

  it('edits a connection: the saved values filled in, a secret left empty kept, the name fixed', async () => {
    const saved = { ...SAVED, inputs: { management_key: '(set)', region: 'us', verify: 'false' } }
    const calls = stubApi(base({ 'GET /api/catalog/connections': { connections: [saved] }, 'POST /api/catalog/connections': saved }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await menu(user, 'Actions for openrouter', 'Edit')
    const dialog = await screen.findByRole('dialog')
    const key = await within(dialog).findByLabelText('Management key') as HTMLInputElement
    expect(key.value).toBe('')
    expect(key.placeholder).toBe('set · leave empty to keep')
    expect(within(dialog).getByLabelText('Data region').textContent).toBe('United States')
    expect(isChecked(within(dialog).getByRole('switch', { name: /Verify/ }))).toBe(false)
    expect(within(dialog).queryByLabelText('Name')).toBeNull()
    await choose(user, within(dialog).getByLabelText('Data region'), 'Europe')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      package: 'openrouter-account', key: 'openrouter', inputs: { region: 'eu', verify: 'false' },
    }))
  })

  it('says a connection cannot connect when its check failed', async () => {
    stubApi(base({ 'GET /api/catalog/connections': { connections: [{ ...SAVED, check_status: 'failed' }] } }))
    renderApp(<ServicesPage />)
    const row = (await (await group('Connected')).findByText('OpenRouter account')).closest('li')!
    expect(within(row).getByText('Can’t connect')).toBeTruthy()
  })

  it('adds another connection of the same package, refusing a name that is taken', async () => {
    const calls = stubApi(base({ 'GET /api/catalog/connections': { connections: [SAVED] }, 'POST /api/catalog/connections': {} }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await menu(user, 'Actions for openrouter', 'Add another')
    const dialog = await screen.findByRole('dialog')
    await user.type(await within(dialog).findByLabelText('Management key'), 'fixture-key')
    await user.click(within(dialog).getByRole('button', { name: 'More options' }))
    const name = within(dialog).getByLabelText('Name') as HTMLInputElement
    expect(name.value).toBe('openrouter-2')
    await user.clear(name)
    await user.type(name, 'openrouter')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    expect(await within(dialog).findByText('A connection named openrouter exists already; edit it instead.')).toBeTruthy()
    expect(calls.some(c => c.method === 'POST')).toBe(false)
  })

  it('adds a package that runs on the connection to a repository that does not have it yet', async () => {
    const calls = stubApi(base({
      'GET /api/catalog/connections': { connections: [SAVED] },
      'GET /api/catalog': { packages: [ACCOUNT, { ...KEY, inputs: { limit: { required: true, secret: false, agent: true, help: '', label: 'Credit limit (USD)', type: 'number', min: 0 } } }], problems: [] },
      'GET /api/repos': { repos: [{ repo: 'acme/app', trust: 'trusted' }, { repo: 'acme/api', trust: 'trusted' }, { repo: 'oss/lib', trust: 'public' }] },
      'POST /api/repos/acme/api/attachments': { attachment: {} },
    }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await menu(user, 'Actions for openrouter', 'Use in a repository')
    const dialog = await screen.findByRole('dialog')
    const repo = await within(dialog).findByLabelText('Repository')
    expect(await optionNames(user, repo)).toEqual(['Choose…', 'acme/app (has it)', 'acme/api'])
    await choose(user, repo, 'acme/api')
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    await user.type(await within(dialog).findByLabelText('Credit limit (USD)'), '5')
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({ package: 'openrouter-key', inputs: { limit: '5' } }))
  })
})

const agent = (kind: string, title: string, state: Connection['state'], extra: Partial<Connection> = {}): Connection => ({
  kind, title, group: 'agent', state, summary: '', display: [], facts: {}, actions: ['connect', 'test', 'disconnect'],
  fields: [{ name: 'token', label: 'Token', secret: true, set: state !== 'not_connected', value: null, help: '' }], ...extra,
})

describe('servers this host runs', () => {
  const RUNNABLE = { ...SERVER, server: true }
  const MANAGED = { name: 'dev-pg', kind: 'postgres', package: 'postgres-server', status: 'ready', error: '', address: '172.29.50.10', port: 5432, used_by: [] }
  const catalog = { 'GET /api/catalog': { packages: [RUNNABLE], problems: [] } }

  it('runs a package\'s server here by default, asking only for its name', async () => {
    const calls = stubApi(base({ ...catalog, 'GET /api/servers': { servers: [], packages: [] }, 'POST /api/servers': { ...MANAGED, status: 'preparing' } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add Postgres server' }))
    const dialog = await screen.findByRole('dialog')
    expect(isChecked(within(dialog).getByRole('radio', { name: /Run it on this server/ }))).toBe(true)
    expect(within(dialog).queryByLabelText('URL')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Run' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({ name: 'postgres', package: 'postgres-server' })
  })

  it('still connects one the owner runs, with the package\'s inputs', async () => {
    stubApi(base({ ...catalog, 'GET /api/servers': { servers: [], packages: [] } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add Postgres server' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('radio', { name: /Connect one you run/ }))
    expect(await within(dialog).findByLabelText('URL')).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: 'Connect' })).toBeTruthy()
  })

  it('shows one that failed to start with why, to start again or remove', async () => {
    const calls = stubApi(base({ ...catalog, 'GET /api/servers': { servers: [{ ...MANAGED, status: 'failed', error: 'docker compose up failed (exit 1)' }], packages: [] }, 'POST /api/servers': { ...MANAGED, status: 'preparing' } }))
    renderApp(<ServicesPage />)
    const row = (await (await group('Connected')).findByText('dev-pg')).closest('li')!
    expect(within(row).getByText('Failed to start')).toBeTruthy()
    expect(within(row).getByText('docker compose up failed (exit 1)')).toBeTruthy()
    await menu(userEvent.setup(), 'Actions for dev-pg', 'Start again')
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({ name: 'dev-pg', package: 'postgres-server' }))
  })

  it('removes a server with its connection, saying that its data goes', async () => {
    const saved = { ...SAVED, key: 'dev-pg', package: 'postgres-server', title: 'Postgres server', managed: { kind: 'postgres', status: 'ready' } }
    const calls = stubApi(base({ ...catalog, 'GET /api/catalog/connections': { connections: [saved] }, 'GET /api/servers': { servers: [MANAGED], packages: [] },
      'DELETE /api/servers/dev-pg': { name: 'dev-pg', stopped: true, data_deleted: true } }))
    renderApp(<ServicesPage />)
    const user = userEvent.setup()
    const row = (await (await group('Connected')).findByText('dev-pg')).closest('li')!
    expect(within(row).getByText(/Runs on this server/)).toBeTruthy()
    await user.click(within(row).getByRole('button', { name: 'Actions for dev-pg' }))
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).toBeNull()   // its address and credentials are the server's
    await user.click(await screen.findByRole('menuitem', { name: 'Remove' }))
    expect(await screen.findByText(/stops the server and deletes its data/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Stop and delete' }))
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE' && c.url === '/api/servers/dev-pg')).toBe(true))
    expect(calls.some(c => c.url.startsWith('/api/catalog/connections/'))).toBe(false)
  })
})

describe('agents and GitHub', () => {
  it('lists every agent with its state: one chosen without its key asks for it, one not set up offers Set up', async () => {
    stubApi(base({ 'GET /api/connections': { connections: [
      agent('amp', 'Amp', 'not_connected', { facts: { chosen: true }, summary: 'In every template; needs its provider token' }),
      agent('claude-code', 'Claude Code', 'off'),
    ] } }))
    renderApp(<ExtensionsPage />)
    const agents = await group('Coding agents')
    const amp = (await agents.findByText('Amp')).closest('li')!
    expect(within(amp).getByText('Needs its key')).toBeTruthy()
    expect(within(amp).getByRole('button', { name: 'Add key' })).toBeTruthy()
    const claude = (await agents.findByText('Claude Code')).closest('li')!
    expect(within(claude).getByText('Not set up')).toBeTruthy()
    expect(within(claude).getByRole('button', { name: 'Set up' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Add' })).toBeNull()   // services have their own page
  })

  it('offers no Edit for a connector with nothing a person changes', async () => {
    stubApi(base({ 'GET /api/connections': { connections: [
      agent('codex', 'Codex', 'connected', { actions: ['connect', 'test'], fields: [{ name: 'version', label: 'Codex version', secret: false, set: false, value: '1.0', help: '', hidden: true }] }),
    ] } }))
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Actions for Codex' }))
    expect(await screen.findByRole('menuitem', { name: 'Test' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).toBeNull()
  })

  it('creates the GitHub App from its Set up dialog, the organization optional', async () => {
    const github: Connection = { kind: 'github', title: 'GitHub', group: 'platform', state: 'not_connected', summary: 'No App yet', display: [], facts: {},
      actions: ['connect', 'test', 'disconnect'], fields: [{ name: 'id', label: 'App ID', secret: false, set: false, value: '', help: '', kind: 'int' }] }
    const calls = stubApi(base({ 'GET /api/connections': { connections: [github] }, 'POST /api/connections/github/manifest': { action_url: 'https://github.com/settings/apps/new', manifest: '{}' } }))
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(within((await screen.findByText('GitHub', { selector: 'span' })).closest('li')!).getByRole('button', { name: 'Set up' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByLabelText('App ID')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: /Create on GitHub/ }))
    await waitFor(() => expect(submit).toHaveBeenCalled())
    expect(calls.find(c => c.method === 'POST')?.url).toBe('/api/connections/github/manifest')
    submit.mockRestore()
  })
})

describe('a package\'s settings for a repository', () => {
  it('shows the error with a retry instead of a form while connections cannot be loaded', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': () => ({ status: 500, body: { detail: 'the store is down' } }),
      'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
    })
    renderApp(<AttachForm repo="acme/api" entry={KEY as never} onDone={() => {}} />)
    expect(await screen.findByRole('button', { name: /Retry/ })).toBeTruthy()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('asks for no connection when there is only one to use', async () => {
    stubApi({ 'GET /api/catalog/connections': { connections: [SAVED] }, 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] } })
    renderApp(<AttachForm repo="acme/api" entry={KEY as never} onDone={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Add' })).toBeTruthy()
    await waitFor(() => expect(screen.queryByLabelText('OpenRouter account')).toBeNull())
  })

  it('sends a choice set back to blank and leaves a saved secret out', async () => {
    const calls = stubApi({ 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] }, 'POST /api/repos/acme/app/attachments': { attachment: {} } })
    const entry = { ...KEY, requires: {}, inputs: {
      limit_reset: { required: false, secret: false, agent: true, help: '', default: '', label: 'Limit resets', choices: [{ value: '', label: 'Never' }, { value: 'daily', label: 'Daily' }] },
      token: { required: false, secret: true, agent: true, help: '' },
    } }
    const attachment = { key: 'openrouter', package: 'openrouter-key', kind: 'provision', title: 'OpenRouter API key', logo: '', origin: 'here', inputs: { limit_reset: 'daily' },
      secret_inputs: { token: true }, connections: {}, exports: [], disabled: false, state: 'ready', problem: null, project: false }
    renderApp(<AttachForm repo="acme/app" entry={entry as never} attachment={attachment as never} onDone={() => {}} />)
    const user = userEvent.setup()
    expect((await screen.findByLabelText(/^Token/) as HTMLInputElement).placeholder).toBe('set · leave empty to keep')
    await choose(user, await screen.findByLabelText('Limit resets'), 'Never')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({ inputs: { limit_reset: '' } }))
    expect((calls.find(c => c.method === 'POST')!.body as { inputs: Record<string, string> }).inputs).not.toHaveProperty('token')
  })

  it('folds an advanced input under More options', async () => {
    stubApi({ 'GET /api/catalog': { packages: [], problems: [] } })
    const entry = { ...KEY, requires: {}, inputs: { pool: { required: false, secret: false, agent: true, help: '', default: '20', type: 'integer', min: 1, advanced: true, label: 'Pool' } } }
    renderApp(<AttachForm repo="acme/app" entry={entry as never} onDone={() => {}} />)
    expect(screen.queryByLabelText(/^Pool/)).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'More options' }))
    expect((screen.getByLabelText(/^Pool/) as HTMLInputElement).type).toBe('number')
  })
})

describe('web addresses in text', () => {
  it('links real addresses and leaves example values as text', () => {
    const links = (text: string) => webAddresses(text).map(a => [a.text, a.href])
    expect(links('A management key from openrouter.ai/settings/management-keys, not an API key.'))
      .toEqual([['openrouter.ai/settings/management-keys', 'https://openrouter.ai/settings/management-keys']])
    expect(links('See https://example.com/docs.')).toEqual([['https://example.com/docs', 'https://example.com/docs']])
    expect(links('postgresql://role:password@db.example.com:5432/postgres, a role')).toEqual([])
    expect(links('HTTP(S) endpoint, such as http://host:8123')).toEqual([])
    expect(links('the address http://10.10.0.20:3010')).toEqual([])
    expect(links('mail ops@example.com/x')).toEqual([])
  })
})

describe('package words', () => {
  it('names inputs in words, writes help as sentences and finds the summary', () => {
    expect(inputLabel('management_key')).toBe('Management key')
    expect(inputLabel('include_byok_in_limit')).toBe('Include BYOK in limit')
    expect(inputLabel('role_arn')).toBe('Role ARN')
    expect(inputLabel('limit', { label: 'Credit limit (USD)' })).toBe('Credit limit (USD)')
    expect(sentence('the frontend, host:7233')).toBe('The frontend, host:7233.')
    expect(sentence('postgresql://role@host/db, a role')).toBe('postgresql://role@host/db, a role.')
    expect(summary('A key per sandbox. Deleted with it.')).toBe('A key per sandbox.')
    expect(suggestKey('openrouter-account', ['openrouter'])).toBe('openrouter-2')
  })
})
