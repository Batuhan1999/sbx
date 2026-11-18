import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { SetupState } from '@/api/types'
import { AdminStep } from '@/pages/setup/SetupPage'
import { renderApp, stubApi } from './render'

const SETUP: SetupState = {
  complete: false,
  steps: [{ step: 'sign_in', done: false, optional: false, summary: '' }],
  signed_in: false, email: null, open_access: false, agents: [], can_complete: false,
  host: { address: '203.0.113.7' },
  access: { mode: 'ip', domain: '', url: '', certificate: '', certificate_email: '', certificate_ready: false, certificate_error: '', needs_ports: [] },
  link: 'http://203.0.113.7/app/setup', admins: [], version: 'dev',
}

describe('setup admin step', () => {
  // The button sits in the step's footer, outside the <form>: it submits only through the form attribute.
  it('creates the account when "Create account" is clicked', async () => {
    const calls = stubApi({ 'POST /api/setup/admin': { ...SETUP, signed_in: true, email: 'owner@example.com' } })
    const onNext = vi.fn()
    renderApp(<AdminStep s={SETUP} onBack={() => {}} onNext={onNext} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Admin email'), 'owner@example.com')
    await user.type(screen.getByLabelText('Password'), 'correct horse')
    await user.type(screen.getByLabelText('Repeat password'), 'correct horse')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    await waitFor(() => expect(onNext).toHaveBeenCalled())
    expect(calls.find(c => c.method === 'POST')).toMatchObject({ url: '/api/setup/admin', body: { email: 'owner@example.com', password: 'correct horse' } })
  })
})
