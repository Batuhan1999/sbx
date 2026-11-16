import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ExtensionsPage from '@/pages/extensions/ExtensionsPage'
import { ME, renderApp, stubApi } from './render'

const LOGO = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4='
const pkg = (name: string, title: string, kind: 'connection' | 'provision', logo = '') => ({
  name, title, kind, logo, description: `${title} for sandboxes`, source: 'shipped', inputs: {}, outputs: {}, env: {},
  requires: {}, runs: {}, checks: false, docs: '', used_by: [],
})
const SERVER = pkg('postgres-server', 'Postgres server', 'connection', LOGO)
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
    expect(await within(dialog).findByLabelText('Key')).toBeTruthy()
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
    await user.click(await within(dialog).findByRole('button', { name: 'Check and save' }))
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Back' })).toHaveProperty('disabled', true))
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
