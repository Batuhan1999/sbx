import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ConnectRepoPage from '@/pages/repos/ConnectRepoPage'
import { ME, renderApp, stubApi } from './render'

const Where = () => <output aria-label="where">{useLocation().pathname}</output>
const github = (state: string) => ({ connections: [{ kind: 'github', title: 'GitHub', group: 'platform', state, summary: state === 'connected' ? 'App sbx-acme' : 'No App yet', display: [],
  facts: state === 'connected' ? { slug: 'sbx-acme', install_url: 'https://github.com/apps/sbx-acme/installations/new' } : {}, actions: [], fields: [] }] })
const REPOS = { repositories: [
  { repo: 'acme/web', description: 'The storefront', private: true, branch: 'main', pushed_at: new Date(Date.now() - 2 * 3600_000).toISOString() },
  { repo: 'acme/api', description: '', private: false, branch: 'trunk', pushed_at: '' },
  { repo: 'acme/docs', description: 'Handbook', private: false, branch: 'main', pushed_at: '' },
] }
afterEach(() => vi.restoreAllMocks())

describe('connecting a repository', () => {
  it('lists what the GitHub App can see, with a search and the way to let it see more', async () => {
    const user = userEvent.setup()
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('connected'), 'GET /api/repos': { repos: [{ repo: 'acme/docs', trust: 'app' }] }, 'GET /api/github/repositories': REPOS,
      'GET /api/github/search': { repositories: [] } })
    renderApp(<ConnectRepoPage />)
    const list = within(await screen.findByRole('list', { name: 'GitHub repositories' }))
    expect(list.getAllByRole('listitem').map(li => li.querySelector('p')?.textContent)).toEqual(['acme/web', 'acme/api', 'acme/docs'])
    expect(list.getByText(/ago$/)).toBeTruthy()   // when it was last pushed
    expect(list.getByLabelText('private')).toBeTruthy()
    expect(list.getAllByRole('button', { name: 'Connect' })).toHaveLength(2)
    expect(list.getByRole('link', { name: /Open/ }).getAttribute('href')).toBe('/app/repos/acme/docs')   // connected already
    expect(screen.getByRole('link', { name: /Adjust GitHub App permissions/ }).getAttribute('href')).toBe('https://github.com/apps/sbx-acme/installations/new')

    await user.type(screen.getByRole('searchbox', { name: 'Search or paste a repository' }), 'we')
    expect(list.getAllByRole('listitem').map(li => li.querySelector('p')?.textContent)).toEqual(['acme/web'])
    await user.clear(screen.getByRole('searchbox', { name: 'Search or paste a repository' }))
    await user.type(screen.getByRole('searchbox', { name: 'Search or paste a repository' }), 'nothing')
    expect(await screen.findByText('No match for “nothing”.')).toBeTruthy()   // nor on GitHub
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy()
  })

  it('connects one with a click and opens it', async () => {
    const user = userEvent.setup()
    const calls = stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('connected'), 'GET /api/repos': { repos: [] }, 'GET /api/github/repositories': REPOS,
      'POST /api/repo-onboarding/connect': { repo: 'acme/web' } })
    renderApp(<><ConnectRepoPage /><Where /></>, '/app/repos/new')
    const row = (await screen.findByText('web')).closest('li')!
    await user.click(within(row).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/repos/acme/web'))
    expect(calls.filter(c => c.method === 'POST').map(c => c.url)).toEqual(['/api/repo-onboarding/connect'])
  })

  it('asks to connect GitHub first, and the same field takes a pasted public repository', async () => {
    const user = userEvent.setup()
    const calls = stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('not_connected'), 'GET /api/repos': { repos: [] },
      'POST /api/repo-onboarding/connect': { repo: 'octo/hello' } })
    renderApp(<><ConnectRepoPage /><Where /></>, '/app/repos/new')
    expect(await screen.findByText('Connect GitHub to import private repositories.')).toBeTruthy()
    expect(screen.getByRole('link', { name: /Connect GitHub/ }).getAttribute('href')).toBe('/app/extensions#github')
    expect(screen.getAllByRole('searchbox')).toHaveLength(1)   // one field, no second one for addresses
    await user.type(screen.getByRole('searchbox', { name: 'Search or paste a repository' }), 'https://github.com/Octo/Hello.git')
    const pasted = within(screen.getByRole('list', { name: 'Pasted repository' }))
    expect(pasted.getByText('hello')).toBeTruthy()
    await user.click(pasted.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(screen.getByRole('status', { name: 'where' }).textContent).toBe('/app/repos/octo/hello'))
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({ repo: 'octo/hello' })
  })

  it('finds a pasted URL in the list instead of adding a row for it', async () => {
    const user = userEvent.setup()
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('connected'), 'GET /api/repos': { repos: [] }, 'GET /api/github/repositories': REPOS })
    renderApp(<ConnectRepoPage />)
    await screen.findByRole('list', { name: 'GitHub repositories' })
    await user.type(screen.getByRole('searchbox', { name: 'Search or paste a repository' }), 'https://github.com/acme/api')
    const list = within(screen.getByRole('list', { name: 'GitHub repositories' }))
    expect(list.getAllByRole('listitem').map(li => li.querySelector('p')?.textContent)).toEqual(['acme/api'])
    expect(screen.queryByRole('list', { name: 'Pasted repository' })).toBeNull()
  })

  it('finds public repositories on GitHub by name, under the App’s own', async () => {
    const user = userEvent.setup()
    const calls = stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('connected'), 'GET /api/repos': { repos: [] }, 'GET /api/github/repositories': REPOS,
      'GET /api/github/search': { repositories: [
        { repo: 'vercel/next.js', description: '', private: false, branch: 'canary', pushed_at: '' },
        { repo: 'acme/web', description: '', private: false, branch: 'main', pushed_at: '' }] } })
    renderApp(<ConnectRepoPage />)
    await screen.findByRole('list', { name: 'GitHub repositories' })
    await user.type(screen.getByRole('searchbox', { name: 'Search or paste a repository' }), 'web')
    const hits = within(await screen.findByRole('list', { name: 'Public repositories' }))
    expect(hits.getAllByRole('listitem').map(li => li.querySelector('p')?.textContent)).toEqual(['vercel/next.js'])   // acme/web is the App's, above
    expect(hits.getByRole('button', { name: 'Connect' })).toBeTruthy()
    expect(calls.filter(c => c.url.startsWith('/api/github/search')).map(c => c.url)).toEqual(['/api/github/search?q=web'])   // once, after the pause
  })

  it('searches GitHub without a GitHub App too, and says when GitHub’s limit is reached', async () => {
    const user = userEvent.setup()
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('not_connected'), 'GET /api/repos': { repos: [] },
      'GET /api/github/search': () => ({ status: 429, body: { detail: 'GitHub’s search limit is reached. Try again in a minute.' } }) })
    renderApp(<ConnectRepoPage />)
    await user.type(await screen.findByRole('searchbox', { name: 'Search or paste a repository' }), 'next')
    expect(await screen.findByText('GitHub’s search limit is reached. Try again in a minute.')).toBeTruthy()
    expect(screen.queryByText('Connect GitHub to import private repositories.')).toBeNull()
  })

  it('shows the last pushed few before a search, and all of them on request', async () => {
    const user = userEvent.setup()
    const many = { repositories: Array.from({ length: 9 }, (_, i) => ({ repo: `acme/r${i}`, description: '', private: false, branch: 'main', pushed_at: '' })) }
    stubApi({ 'GET /api/me': ME, 'GET /api/connections': github('connected'), 'GET /api/repos': { repos: [] }, 'GET /api/github/repositories': many,
      'GET /api/github/search': { repositories: [] } })
    renderApp(<ConnectRepoPage />)
    const list = () => within(screen.getByRole('list', { name: 'GitHub repositories' })).getAllByRole('listitem').length
    await screen.findByRole('list', { name: 'GitHub repositories' })
    expect(list()).toBe(6)
    await user.click(screen.getByRole('button', { name: 'Show all 9' }))
    expect(list()).toBe(9)
  })
})
