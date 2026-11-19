import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import SandboxesPage from '@/pages/sandboxes/SandboxesPage'
import { ME, renderApp, stubApi } from './render'

const EMPTY = { sandboxes: [], capacity: { known: false, runtime: '' } }
const repo = (configured: boolean) => ({ repo: 'usememos/memos', trust: 'trusted', configured, prebuilt: false, previews: [], secrets_missing: [] })
const page = (repos: unknown[], me = ME) => {
  stubApi({ 'GET /api/me': me, 'GET /api/sandbox-list': EMPTY, 'GET /api/repos': { repos }, 'GET /api/runtimes': { default: 'embed', runtimes: [] } })
  return renderApp(<SandboxesPage />)
}
/** The empty list's own box (the page's header has a New sandbox button of its own). */
const box = async () => within((await screen.findByText('No sandboxes yet')).closest('div')!)

describe('an empty sandboxes page says what would fill it', () => {
  it('sends an admin to the repository first while none is connected', async () => {
    page([])
    const empty = await box()
    expect(await empty.findByText(/A sandbox runs one of your repositories\. Connect one first/)).toBeTruthy()
    expect(empty.getByRole('link', { name: 'Connect a repository' }).getAttribute('href')).toBe('/app/repos/new')
    // Nothing a sandbox could run yet: no button that leads nowhere.
    expect(empty.queryByRole('button', { name: 'New sandbox' })).toBeNull()
  })

  it('points at the repository that still needs its recipe', async () => {
    page([repo(false)])
    const empty = await box()
    expect(await empty.findByText(/none has its recipe yet/)).toBeTruthy()
    expect(empty.getByRole('link', { name: 'Finish a repository' }).getAttribute('href')).toBe('/app/repos')
  })

  it('tells someone who is not an admin whom to ask', async () => {
    page([], { ...ME, admin: false })
    const empty = await box()
    expect(await empty.findByText(/Ask an admin to connect one/)).toBeTruthy()
    expect(empty.queryByRole('link')).toBeNull()
  })

  it('offers the first sandbox once a repository is ready', async () => {
    page([repo(true)])
    const empty = await box()
    expect(await empty.findByText(/sbx up NAME/)).toBeTruthy()
    expect(empty.getByRole('button', { name: 'New sandbox' })).toBeTruthy()
    expect(empty.getByRole('link', { name: 'Use the CLI' }).getAttribute('href')).toBe('/app/cli')
  })
})
