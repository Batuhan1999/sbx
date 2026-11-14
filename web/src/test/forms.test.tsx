import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { NewSandboxForm } from '@/pages/sandboxes/NewSandboxForm'
import { MissingValues } from '@/pages/repos/RepoSetup'
import SignInPage from '@/pages/SignInPage'
import { ME, renderApp, stubApi } from './render'

describe('new sandbox form', () => {
  it('names the sandbox after the branch and validates the name like the server', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/repos': { repos: [] } })
    renderApp(<NewSandboxForm repo="usememos/memos" />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Branch/), 'feature/Login-Fix')
    const name = screen.getByLabelText('Name') as HTMLInputElement
    expect(name.value).toBe('feature-login-fix')
    await user.clear(name)
    await user.type(name, 'Bad name')
    fireEvent.blur(name)
    expect(await screen.findByText('Start with a lowercase letter.')).toBeTruthy()
    expect(name.getAttribute('aria-invalid')).toBe('true')
  })

  it('creates the sandbox with the chosen repository and branch', async () => {
    const calls = stubApi({ 'GET /api/me': ME, 'GET /api/repos': { repos: [] }, 'POST /api/sandboxes': { name: 'login' } })
    renderApp(<NewSandboxForm repo="usememos/memos" />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Branch/), 'login')
    await user.click(screen.getByRole('button', { name: 'Create sandbox' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')).toMatchObject({ url: '/api/sandboxes', body: { name: 'login', branch: 'login', repo: 'usememos/memos', public_keys: '' } })
  })

  it('shows the server’s refusal in the form', async () => {
    stubApi({ 'GET /api/me': ME, 'GET /api/repos': { repos: [] }, 'POST /api/sandboxes': () => ({ status: 409, body: { detail: 'login exists' } }) })
    renderApp(<NewSandboxForm repo="usememos/memos" />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Branch/), 'login')
    await user.click(screen.getByRole('button', { name: 'Create sandbox' }))
    expect((await screen.findByRole('alert')).textContent).toContain('login exists')
  })
})

describe('missing values', () => {
  it('needs at least one value, then stores each as a secret', async () => {
    const calls = stubApi({ 'PUT /api/repos/umami-software/umami/env/APP_SECRET': { name: 'APP_SECRET', secret: true, running: 1, paused: 0 } })
    renderApp(<MissingValues repo="umami-software/umami" names={['APP_SECRET', 'MAXMIND_KEY']} admin />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Save values' }))
    expect(await screen.findByText('Enter at least one value.')).toBeTruthy()
    expect(calls).toHaveLength(0)
    await user.type(screen.getByLabelText('APP_SECRET'), 's3cret-value')
    await user.click(screen.getByRole('button', { name: 'Save values' }))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toMatchObject({ method: 'PUT', body: { value: 's3cret-value', secret: true, agent: false } })
    await waitFor(() => expect((screen.getByLabelText('APP_SECRET') as HTMLInputElement).value).toBe(''))
  })

  it('tells someone who is not an admin who sets them, instead of a form the server refuses', () => {
    stubApi({})
    renderApp(<MissingValues repo="umami-software/umami" names={['APP_SECRET']} admin={false} />)
    expect(screen.queryByRole('button', { name: 'Save values' })).toBeNull()
    expect(screen.getByText(/An admin of this server sets these/)).toBeTruthy()
  })
})

describe('sign in', () => {
  it('validates before sending and shows the server’s answer', async () => {
    const calls = stubApi({ 'POST /auth/login': () => ({ status: 401, body: { detail: 'The email or password is incorrect.' } }) })
    renderApp(<SignInPage />, '/app/sign-in')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Enter a valid email address.')).toBeTruthy()
    expect(calls).toHaveLength(0)
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Password'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect((await screen.findByRole('alert')).textContent).toContain('The email or password is incorrect.')
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('wrong-password')
  })
})
