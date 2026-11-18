import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Connection } from '@/api/types'
import CliPage from '@/pages/CliPage'
import { ConnectionForm } from '@/pages/extensions/ConnectionCard'
import { AgentHandoff } from '@/pages/repos/AgentHandoff'
import { Connect } from '@/pages/sandboxes/SandboxPage'
import { ME, renderApp, stubApi } from './render'

const AMP_ONLY = { ...ME, agents: { amp: { title: 'Amp', command: 'amp' } } }
const commands = () => screen.getAllByText((_, el) => el?.tagName === 'CODE').map(el => el.textContent?.replace(/^\$/, ''))

describe('agent commands come from the server', () => {
  it('offers the agents /api/me lists, and no Claude on a server without it', async () => {
    stubApi({ 'GET /api/me': AMP_ONLY })
    renderApp(<Connect s={{ name: 'feat' }} />)
    expect(await screen.findByText('sbx open feat amp')).toBeTruthy()
    expect(commands().some(c => c?.includes('claude'))).toBe(false)
  })

  it('words the CLI page and the handoff from the same list', async () => {
    stubApi({ 'GET /api/me': AMP_ONLY })
    renderApp(<><CliPage /><AgentHandoff repo="usememos/memos" configured={false} /></>)
    expect(await screen.findAllByText('sbx repo connect --agent amp')).toHaveLength(2)
    expect(screen.getByText(/Starts Amp with the same prompt/)).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/claude|Claude Code/)
  })
})

describe('saving an agent connector', () => {
  const AMP: Connection = { kind: 'amp', title: 'Amp', group: 'agent', state: 'off', summary: 'Provider stopped', display: [], facts: {}, actions: ['connect', 'disconnect'],
    fields: [{ name: 'enabled', label: 'Enabled', secret: false, set: false, value: 'false', help: '' }] }

  it('says how many templates the change rebuilds and keeps the dialog open to read it', async () => {
    const rebuilds = { runtimes: { embed: 1 }, keys: { embed: ['default'] }, building: true, message: '1 template rebuilds (E2B Embed: 1), about 10 minutes each.' }
    const calls = stubApi({ 'PUT /api/connections/amp': { ...AMP, state: 'connected', rebuilds } })
    const done = vi.fn()
    renderApp(<ConnectionForm c={AMP} onDone={done} />)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Test and save' }))
    expect((await screen.findByRole('status')).textContent).toBe('Saved. 1 template rebuilds (E2B Embed: 1), about 10 minutes each.')
    expect(done).not.toHaveBeenCalled()
    expect(calls[0].body).toEqual({ values: { enabled: 'false' } })
  })

  it('closes at once when no template changes', async () => {
    stubApi({ 'PUT /api/connections/amp': { ...AMP, rebuilds: { runtimes: {}, keys: {}, building: false, message: 'No template changes; nothing rebuilds.' } } })
    const done = vi.fn()
    renderApp(<ConnectionForm c={AMP} onDone={done} />)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Test and save' }))
    await waitFor(() => expect(done).toHaveBeenCalled())
  })
})
