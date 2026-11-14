import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AgentHandoff } from '@/pages/repos/AgentHandoff'
import { renderApp, stubApi } from './render'

const BRIEF = '# Set up usememos/memos on https://sbx.example.com\n\nYou are in a checkout…'

describe('copy prompt', () => {
  it('fetches the setup brief and puts all of it on the clipboard in one click', async () => {
    const calls = stubApi({ 'GET /api/repos/usememos/memos/setup-brief': BRIEF })
    const user = userEvent.setup()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderApp(<AgentHandoff repo="usememos/memos" configured={false} />)
    await user.click(screen.getByRole('button', { name: 'Copy prompt' }))
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeTruthy()
    expect(writeText).toHaveBeenCalledWith(BRIEF)
    expect(calls.map(c => `${c.method} ${c.url}`).filter(c => c !== 'GET /api/me')).toEqual(['GET /api/repos/usememos/memos/setup-brief'])
  })

  it('shows the prompt in a dialog when the browser has no Clipboard API', async () => {
    stubApi({ 'GET /api/repos/usememos/memos/setup-brief': BRIEF })
    const user = userEvent.setup()
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    renderApp(<AgentHandoff repo="usememos/memos" configured={false} />)
    await user.click(screen.getByRole('button', { name: 'Copy prompt' }))
    const dialog = await screen.findByRole('dialog')
    expect((within(dialog).getByLabelText('Setup prompt') as HTMLTextAreaElement).value).toBe(BRIEF)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull())
  })
})
