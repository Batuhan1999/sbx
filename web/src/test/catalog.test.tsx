import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ExtensionsPage from '@/pages/extensions/ExtensionsPage'
import { webAddresses } from '@/components/LinkedText'
import { AttachForm } from '@/components/AttachForm'
import { inputLabel, providers, sentence, sentences, summary, suggestKey } from '@/lib/packages'
import { ME, renderApp, stubApi } from './render'

const LOGO = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4='
const pkg = (name: string, title: string, kind: 'connection' | 'provision', logo = '') => ({
  name, title, kind, logo, description: `${title} for sandboxes`, source: 'shipped', inputs: {}, outputs: {}, env: {},
  requires: {}, runs: {}, checks: false, docs: '', used_by: [],
})
const SERVER = { ...pkg('postgres-server', 'Postgres server', 'connection', LOGO), inputs: { url: { required: true, secret: true, agent: true, help: 'an admin URL' } } }
const CATALOG = { packages: [SERVER, pkg('static', 'Static values', 'provision')], problems: [] }

describe('package logos', () => {
  it('shows each package its own logo, and the generic icon without one', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] }, 'GET /api/catalog': CATALOG })
    renderApp(<ExtensionsPage />)
    const card = (await screen.findByText('Postgres server')).closest('button')!
    expect(card.querySelector('img')?.getAttribute('src')).toBe(LOGO)
    const plain = screen.getByText('Static values').closest('button')!
    expect(plain.querySelector('img')).toBeNull()
    expect(plain.querySelector('svg')).toBeTruthy()
  })

  it('picks a connection kind by its logo and title, and goes back to the list', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
      'GET /api/catalog/postgres-server': { ...SERVER, readme: '', connections: [], scripts: [] }, 'GET /api/catalog': CATALOG,
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByText('Static values')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: /Postgres server/ }))
    expect(await within(dialog).findByLabelText('Name')).toBeTruthy()
    expect(dialog.querySelector('img')?.getAttribute('src')).toBe(LOGO)
    await user.click(within(dialog).getByRole('button', { name: 'Back' }))
    expect(await within(dialog).findByRole('button', { name: /Postgres server/ })).toBeTruthy()
  })

  it('keeps Back disabled while a connection is being saved', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
      'GET /api/catalog/postgres-server': { ...SERVER, readme: '', connections: [], scripts: [] }, 'GET /api/catalog': CATALOG,
      'POST /api/catalog/connections': { key: 'postgres-server' },
    })
    const answer = globalThis.fetch
    let release = () => {}
    vi.stubGlobal('fetch', (url: string, init: RequestInit = {}) => init.method === 'POST'
      ? new Promise(resolve => { release = () => resolve(answer(url, init)) }) : answer(url, init))
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /Postgres server/ }))
    await user.type(await within(dialog).findByLabelText('URL'), 'postgresql://admin@fixture/db')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Back' })).toHaveProperty('disabled', true))
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

const ACCOUNT = {
  ...pkg('openrouter-account', 'OpenRouter account', 'connection', LOGO), description: 'An OpenRouter account. It stays here.', docs: 'https://example.com/keys',
  inputs: {
    management_key: { required: true, secret: true, agent: true, help: 'a management key' },
    region: { required: false, secret: false, agent: true, help: '', default: 'eu', label: 'Data region', choices: [{ value: 'eu', label: 'Europe' }, { value: 'us', label: 'United States' }] },
    verify: { required: false, secret: false, agent: true, help: 'check the key first', default: 'true' },
  },
}
const KEY = { ...pkg('openrouter-key', 'OpenRouter API key', 'provision'), description: 'An API key per sandbox. Deleted with it.', requires: { account: { kind: 'openrouter-account' } }, used_by: ['acme/app'] }

describe('the catalog for people', () => {
  it('shows one card per provider, with what each sandbox gets and whether it is connected', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] },
      'GET /api/catalog/connections': { connections: [{ key: 'openrouter', package: 'openrouter-account', title: 'OpenRouter account', logo: LOGO, inputs: {}, used_by: ['acme/app:openrouter'], known: true, check_status: 'passed' }] },
      'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
      'GET /api/repos': { repos: [{ repo: 'acme/app', trust: 'trusted' }, { repo: 'acme/api', trust: 'trusted' }, { repo: 'oss/lib', trust: 'public' }] },
    })
    renderApp(<ExtensionsPage />)
    const catalog = within((await screen.findByRole('heading', { name: 'Package catalog' })).closest('section')!)
    const card = (await catalog.findByText('OpenRouter account')).closest('button')!
    expect(within(card).getByText('An API key per sandbox.')).toBeTruthy()
    expect(await within(card).findByText('Connected')).toBeTruthy()
    expect(within(card).getByText('In 1 repository')).toBeTruthy()
    expect(catalog.queryByRole('button', { name: /OpenRouter API key/ })).toBeNull()
    await userEvent.click(card)
    const panel = await screen.findByRole('dialog')
    const step = (title: string) => within(panel).getByText(title).closest('li')!
    expect(step('Connect OpenRouter account').getAttribute('aria-current')).toBeNull()
    expect(within(step('Connect OpenRouter account')).getByText(/openrouter/)).toBeTruthy()
    expect(step('Choose a repository').getAttribute('aria-current')).toBe('step')
    const links = await within(panel).findAllByRole('link', { name: /acme\/app/ })
    expect(new Set(links.map(a => a.getAttribute('href')))).toEqual(new Set(['/app/repos/acme/app/packages']))
    expect(within(panel).queryByRole('radio', { name: /oss\/lib/ })).toBeNull()
    expect(within(panel).queryByText(/has a check|shared connection|management_key/)).toBeNull()
  })

  it('asks for inputs by label, offers choices as a list and yes or no as a checkbox', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
      'GET /api/catalog/openrouter-account': { ...ACCOUNT, readme: '', connections: [], scripts: [] }, 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
      'POST /api/catalog/connections': { key: 'openrouter' },
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /OpenRouter account/ }))
    await user.type(await within(dialog).findByLabelText('Management key'), 'fixture-key')
    expect(calls.some(c => c.url.startsWith('/api/catalog/openrouter-account'))).toBe(false)
    expect(within(dialog).getByText('A management key.')).toBeTruthy()
    const region = within(dialog).getByLabelText('Data region') as HTMLSelectElement
    expect([...region.options].map(o => o.text)).toEqual(['Europe', 'United States'])
    await user.selectOptions(region, 'us')
    const verify = within(dialog).getByRole('checkbox', { name: /Verify/ }) as HTMLInputElement
    expect(verify.checked).toBe(true)
    await user.click(verify)
    expect((within(dialog).getByLabelText('Name') as HTMLInputElement).value).toBe('openrouter')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      package: 'openrouter-account', key: 'openrouter', inputs: { management_key: 'fixture-key', region: 'us', verify: 'false' },
    }))
  })
})

describe('connecting from the catalog', () => {
  it('asks for the account in the first step while nothing is connected, then for a repository', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
      'GET /api/catalog/openrouter-account': { ...ACCOUNT, readme: '', connections: [], scripts: [] }, 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
      'POST /api/catalog/connections': { key: 'openrouter' },
      'GET /api/repos': { repos: [{ repo: 'acme/app', trust: 'trusted' }, { repo: 'acme/api', trust: 'trusted' }, { repo: 'oss/lib', trust: 'public' }] },
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    const catalog = within((await screen.findByRole('heading', { name: 'Package catalog' })).closest('section')!)
    await user.click((await catalog.findByText('OpenRouter account')).closest('button')!)
    const panel = await screen.findByRole('dialog')
    expect(within(panel).getByText('Connect OpenRouter account').closest('li')!.getAttribute('aria-current')).toBe('step')
    expect(within(panel).getByText('An OpenRouter account.')).toBeTruthy()
    expect(within(panel).getByText('It stays here.').closest('li')).toBeTruthy()
    await user.type(await within(panel).findByLabelText('Management key'), 'fixture-key')
    expect(within(panel).queryByRole('button', { name: 'Back' })).toBeNull()
    await user.click(within(panel).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(within(panel).getByText('Choose a repository').closest('li')!.getAttribute('aria-current')).toBe('step'))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({ package: 'openrouter-account', key: 'openrouter' }))
  })
})

describe('editing a connection', () => {
  it('fills in the saved values, keeps a secret left empty, and keeps the name', async () => {
    const saved = { key: 'openrouter', package: 'openrouter-account', title: 'OpenRouter account', logo: LOGO, used_by: [], known: true, check_status: 'passed',
      inputs: { management_key: '(set)', region: 'us', verify: 'false' } }
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [saved] },
      'GET /api/catalog/openrouter-account': { ...ACCOUNT, readme: '', connections: [], scripts: [] }, 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
      'POST /api/catalog/connections': saved,
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    const row = (await screen.findByText(/^Not used yet/)).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')
    const key = await within(dialog).findByLabelText('Management key') as HTMLInputElement
    expect(key.value).toBe('')
    expect(key.placeholder).toBe('Saved; leave empty to keep it')
    expect((within(dialog).getByLabelText('Data region') as HTMLSelectElement).value).toBe('us')
    expect((within(dialog).getByRole('checkbox', { name: /Verify/ }) as HTMLInputElement).checked).toBe(false)
    expect(within(dialog).queryByLabelText('Name')).toBeNull()
    await user.selectOptions(within(dialog).getByLabelText('Data region'), 'eu')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      package: 'openrouter-account', key: 'openrouter', inputs: { region: 'eu', verify: 'false' },
    }))
  })
})

describe('adding a package from the catalog', () => {
  it('adds a provider\'s package to a repository that does not have it yet', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] },
      'GET /api/catalog/connections': { connections: [{ key: 'openrouter', package: 'openrouter-account', title: 'OpenRouter account', logo: LOGO, inputs: {}, used_by: [], known: true, check_status: 'passed' }] },
      'GET /api/catalog': { packages: [ACCOUNT, { ...KEY, inputs: { limit: { required: true, secret: false, agent: true, help: '', label: 'Credit limit (USD)' } } }], problems: [] },
      'GET /api/repos': { repos: [{ repo: 'acme/app', trust: 'trusted' }, { repo: 'acme/api', trust: 'trusted' }, { repo: 'oss/lib', trust: 'public' }] },
      'POST /api/repos/acme/api/attachments': { attachment: {} },
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    const catalog = within((await screen.findByRole('heading', { name: 'Package catalog' })).closest('section')!)
    await user.click((await catalog.findByText('OpenRouter account')).closest('button')!)
    const panel = await screen.findByRole('dialog')
    expect((await within(panel).findAllByRole('link', { name: /acme\/app/ }))[0].getAttribute('href')).toBe('/app/repos/acme/app/packages')
    expect(within(panel).queryByText('oss/lib')).toBeNull()
    await user.click(within(panel).getByRole('radio', { name: /acme\/api/ }))
    await user.click(within(panel).getByRole('button', { name: 'Continue' }))
    await user.type(await within(panel).findByLabelText('Credit limit (USD)'), '5')
    await user.click(within(panel).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({ package: 'openrouter-key', inputs: { limit: '5' } }))
    expect(await within(panel).findByRole('status')).toHaveProperty('textContent', expect.stringContaining('Added to acme/api'))
    expect(within(panel).getByRole('link', { name: 'Open acme/api' }).getAttribute('href')).toBe('/app/repos/acme/api/packages')
  })
})

describe('when connections fail or cannot be loaded', () => {
  const failed = { key: 'openrouter', package: 'openrouter-account', title: 'OpenRouter account', logo: LOGO, inputs: {}, used_by: [], known: true, check_status: 'failed' }

  it('says a provider cannot connect when its only connection failed its check', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [failed] }, 'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] } })
    renderApp(<ExtensionsPage />)
    const catalog = within((await screen.findByRole('heading', { name: 'Package catalog' })).closest('section')!)
    const card = (await catalog.findByText('OpenRouter account')).closest('button')!
    expect(await within(card).findByText('Can’t connect')).toBeTruthy()
    expect(within(card).queryByText('Connected')).toBeNull()
  })

  it('refuses a name another connection has', async () => {
    const calls = stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [{ ...failed, check_status: 'passed' }] },
      'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] }, 'POST /api/catalog/connections': {},
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /OpenRouter account/ }))
    const name = await within(dialog).findByLabelText('Name') as HTMLInputElement
    expect(name.value).toBe('openrouter-2')
    await user.type(within(dialog).getByLabelText('Management key'), 'fixture-key')
    await user.clear(name)
    await user.type(name, 'openrouter')
    await user.click(within(dialog).getByRole('button', { name: 'Connect' }))
    expect(await within(dialog).findByText('A connection named openrouter exists already; edit it instead.')).toBeTruthy()
    expect(calls.some(c => c.method === 'POST')).toBe(false)
  })

  it('shows the error with a retry instead of a form while connections cannot be loaded', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { status: 500, body: { detail: 'the store is down' } },
      'GET /api/catalog': { packages: [ACCOUNT, KEY], problems: [] },
    })
    renderApp(<AttachForm repo="acme/api" entry={KEY as never} onDone={() => {}} />)
    expect(await screen.findByRole('button', { name: /Retry/ })).toBeTruthy()
    expect(screen.queryByRole('combobox')).toBeNull()
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

  it('shows them as links in a field\'s help', async () => {
    stubApi({
      'GET /api/me': ME, 'GET /api/connections': { connections: [] }, 'GET /api/catalog/connections': { connections: [] },
      'GET /api/catalog': { packages: [{ ...ACCOUNT, inputs: { management_key: { required: true, secret: true, agent: true, help: 'a key from openrouter.ai/settings/keys' } } }, KEY], problems: [] },
    })
    renderApp(<ExtensionsPage />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /OpenRouter account/ }))
    expect((await within(dialog).findByRole('link', { name: 'openrouter.ai/settings/keys' })).getAttribute('href')).toBe('https://openrouter.ai/settings/keys')
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
    expect(sentences('A key per sandbox. The limit caps it; balance pays. Uses ${services.api.base_url}.')).toEqual(['A key per sandbox.', 'The limit caps it; balance pays.', 'Uses ${services.api.base_url}.'])
    expect(suggestKey('openrouter-account', ['openrouter'])).toBe('openrouter-2')
  })

  it('groups packages under the connection they need; others stand alone', () => {
    const lone = pkg('static', 'Static values', 'connection')
    const orphan = { ...pkg('thing', 'Thing', 'provision'), requires: { x: { kind: 'missing' } } }
    const found = providers([ACCOUNT, KEY, lone, orphan] as never)
    expect(found.map(p => [p.title, p.resources.map(r => r.name)])).toEqual([
      ['OpenRouter account', ['openrouter-key']], ['Thing', ['thing']], ['Static values', []],
    ])
  })
})
