import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Sandbox } from '@/api/types'
import { SandboxTable } from '@/pages/sandboxes/SandboxTable'
import { renderApp, stubApi } from './render'

const sandbox = (name: string, extra: Partial<Sandbox> = {}): Sandbox => ({
  name, owner: 'ada@example.com', branch: 'main', sandbox_id: 'id', status: 'up', error: null, urls: {}, app: 'usememos/memos', vm: 'running',
  created_at: null, origin: { kind: 'person', label: 'Started here' }, actions: { pause: true, resume: false, restart: true, remove: true, logs: true }, ...extra,
})

describe('the sandbox list', () => {
  it('links each sandbox’s preview by a short label that names its host', () => {
    stubApi({ 'GET /api/runtimes': { default: '', runtimes: [] } })
    renderApp(<SandboxTable sandboxes={[
      sandbox('login', { urls: { web: 'https://login.sbx.example.com/' } }),
      sandbox('broken', { status: 'failed', urls: { web: 'https://broken.sbx.example.com/' } }),
      sandbox('bare'),
    ]} />)
    // The host does not fit the narrow column it shows in (it ran over the status beside it): the label is short.
    const link = screen.getByRole('link', { name: 'Open preview login.sbx.example.com' })
    expect(link.getAttribute('href')).toBe('https://login.sbx.example.com/')
    expect(link.textContent).toBe('Preview')
    expect(screen.getAllByRole('link', { name: /Open preview/ })).toHaveLength(1)
  })
})
