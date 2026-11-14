import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import CliPage from '@/pages/CliPage'
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
