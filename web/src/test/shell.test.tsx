import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { Shell } from '@/app/Shell'
import { ME, stubApi } from './render'

function renderShell() {
  stubApi({ 'GET /api/me': ME, 'GET /api/updates': { installed: 'dev', check: true, checked_at: '', check_error: '', available: null, upgrade: null } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={['/app/repos']}>
      <Routes><Route path="/app" element={<Shell />}>
        <Route path="repos" element={<h1>Repositories page</h1>} />
        <Route path="sandboxes" element={<h1>Sandboxes page</h1>} />
      </Route></Routes>
    </MemoryRouter>
  </QueryClientProvider>)
}

describe('the navigation drawer', () => {
  it('gives focus back to the button that opened it when it is dismissed', async () => {
    const user = userEvent.setup()
    renderShell()
    const menu = screen.getByRole('button', { name: 'Open navigation' })
    await user.click(menu)
    await screen.findByRole('dialog', { name: 'Navigation' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(menu)
  })

  it('moves focus to the page when a link in it is followed', async () => {
    const user = userEvent.setup()
    renderShell()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const drawer = await screen.findByRole('dialog', { name: 'Navigation' })
    await user.click(within(drawer).getByRole('link', { name: /Sandboxes/ }))
    await screen.findByText('Sandboxes page')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(document.getElementById('main'))
  })
})
