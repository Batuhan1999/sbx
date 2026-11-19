import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Connection } from '@/api/types'
import CliPage from '@/pages/CliPage'
import { ConnectionForm } from '@/pages/extensions/ConnectionCard'
import { AgentHandoff } from '@/pages/repos/AgentHandoff'
import { Connect } from '@/pages/sandboxes/SandboxPage'
import { choose, ME, renderApp, stubApi } from './render'

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
    expect(screen.getByText(/No sbx CLI yet/)).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/claude|Claude Code/)
  })
})

describe('saving an agent connector', () => {
  const AMP: Connection = { kind: 'amp', title: 'Amp', group: 'agent', state: 'off', summary: 'Provider stopped', display: [], facts: {}, actions: ['connect', 'disconnect'],
    fields: [{ name: 'provider_token', label: 'Orb provider token', secret: true, set: true, value: null, help: '' }] }

  it('says how many templates the change rebuilds and keeps the dialog open to read it', async () => {
    const rebuilds = { runtimes: { embed: 1 }, keys: { embed: ['default'] }, building: true, message: '1 template rebuilds (E2B Embed: 1), about 10 minutes each.' }
    const calls = stubApi({ 'PUT /api/connections/amp': { ...AMP, state: 'connected', rebuilds } })
    const done = vi.fn()
    renderApp(<ConnectionForm c={AMP} onDone={done} />)
    const user = userEvent.setup()
    expect((screen.getByLabelText('Orb provider token') as HTMLInputElement).placeholder).toBe('set · leave empty to keep')
    await user.type(screen.getByLabelText('Orb provider token'), 'aop_live_fixture-token')
    await user.click(screen.getByRole('button', { name: 'Test and save' }))
    expect((await screen.findByRole('status')).textContent).toBe('Saved. 1 template rebuilds (E2B Embed: 1), about 10 minutes each.')
    expect(done).not.toHaveBeenCalled()
    expect(calls[0].body).toEqual({ values: { provider_token: 'aop_live_fixture-token' } })
  })

  it('sends only what changed, shows a field by its kind and hides what does not apply', async () => {
    const SIGN_IN: Connection = { kind: 'sign_in', title: 'Sign-in', group: 'platform', state: 'connected', summary: '', display: [], facts: {}, actions: ['connect'],
      fields: [
        { name: 'method', label: 'Method', secret: false, set: false, value: 'password', help: '', kind: 'choice', options: [{ value: 'password', label: 'Passwords' }, { value: 'oidc', label: 'Single sign-on (OIDC)' }] },
        { name: 'oidc_issuer', label: 'Issuer URL', secret: false, set: false, value: '', help: '', kind: 'url', when: { method: ['oidc'] } },
        { name: 'oidc_client_secret', label: 'Client secret', secret: true, set: false, value: null, help: '', when: { method: ['oidc'] } },
        { name: 'version', label: 'Version', secret: false, set: false, value: '1.0', help: '', hidden: true },
      ] }
    const calls = stubApi({ 'PUT /api/connections/sign_in': { ...SIGN_IN, rebuilds: { runtimes: {}, keys: {}, building: false, message: '' } } })
    renderApp(<ConnectionForm c={SIGN_IN} onDone={() => {}} />)
    const user = userEvent.setup()
    expect(screen.queryByLabelText('Issuer URL')).toBeNull()
    expect(screen.queryByLabelText('Version')).toBeNull()
    await choose(user, screen.getByLabelText('Method'), 'Single sign-on (OIDC)')
    const issuer = screen.getByLabelText('Issuer URL') as HTMLInputElement
    expect(issuer.type).toBe('url')
    await user.type(issuer, 'not a url')
    await user.click(screen.getByRole('button', { name: 'Test and save' }))
    expect(await screen.findByText('Enter a URL starting with https://.')).toBeTruthy()
    expect(calls).toHaveLength(0)
    await user.clear(issuer)
    await user.type(issuer, 'https://id.example.com')
    await user.click(screen.getByRole('button', { name: 'Test and save' }))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0].body).toEqual({ values: { method: 'oidc', oidc_issuer: 'https://id.example.com' } })
  })

  it('closes at once when no template changes', async () => {
    stubApi({ 'PUT /api/connections/amp': { ...AMP, rebuilds: { runtimes: {}, keys: {}, building: false, message: 'No template changes; nothing rebuilds.' } } })
    const done = vi.fn()
    renderApp(<ConnectionForm c={AMP} onDone={done} />)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Test and save' }))
    await waitFor(() => expect(done).toHaveBeenCalled())
  })
})
