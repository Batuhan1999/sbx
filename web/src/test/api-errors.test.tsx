import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError, describeError, errorText, request } from '@/api/client'
import { ErrorState, splitDetail } from '@/components/states'
import { stubApi } from './render'

describe('request', () => {
  it('raises ApiError with the status and the server’s detail', async () => {
    stubApi({ 'GET /api/repos/a/b/env': () => ({ status: 400, body: { detail: 'a/b is not a trusted repo here' } }) })
    const error = await request<never>('GET', '/api/repos/a/b/env').catch((e: ApiError) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(400)
    expect(error.message).toBe('a/b is not a trusted repo here')
  })

  it('joins FastAPI validation errors into one message', async () => {
    stubApi({ 'POST /api/sandboxes': () => ({ status: 422, body: { detail: [{ loc: ['body', 'name'], msg: 'Field required' }, { msg: 'String should match pattern' }] } }) })
    const error = await request<never>('POST', '/api/sandboxes', {}).catch((e: ApiError) => e)
    expect(error.message).toBe('Field required; String should match pattern')
  })

  it('returns text for text routes', async () => {
    stubApi({ 'GET /api/repos/a/b/setup-brief': '# Set up a/b' })
    expect(await request('GET', '/api/repos/a/b/setup-brief', undefined, 'text')).toBe('# Set up a/b')
  })
})

describe('describeError', () => {
  it('keeps only the failed checks a refusal carries', () => {
    const e = new ApiError(400, { message: 'GitHub refused the connection', checks: [{ name: 'app id', ok: true, detail: 'found', fix: '' }, { name: 'private key', ok: false, detail: 'not a PEM key', fix: 'paste the .pem file' }] })
    expect(describeError(e)).toMatchObject({ message: 'GitHub refused the connection', status: 400 })
    expect(describeError(e).checks.map(c => c.name)).toEqual(['private key'])
    expect(errorText(e)).toBe('GitHub refused the connection: private key: not a PEM key')
  })

  it('words errors without a message', () => {
    expect(describeError(new ApiError(403, null)).message).toBe('You do not have access to this.')
    expect(describeError(new ApiError(502, null)).message).toMatch(/could not complete/)
    expect(describeError(new TypeError('Failed to fetch')).message).toMatch(/Could not reach the server/)
  })
})

describe('ErrorState', () => {
  it('renders the message, failed checks with their fixes, and a retry', () => {
    let retried = false
    render(<ErrorState error={new ApiError(400, { message: 'The server needs attention.', checks: [{ name: 'Virtualization', ok: false, detail: 'KVM is unavailable', fix: 'Enable nested virtualization.' }] })} retry={() => { retried = true }} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('The server needs attention.')
    expect(alert.textContent).toContain('Virtualization')
    expect(alert.textContent).toContain('Fix: Enable nested virtualization.')
    screen.getByRole('button', { name: /Retry/ }).click()
    expect(retried).toBe(true)
  })

  it('renders nothing without an error', () => {
    const { container } = render(<ErrorState error={null} />)
    expect(container.textContent).toBe('')
  })
})

describe('technical detail', () => {
  it('keeps what is wrong in front and puts addresses and exception text under Details', () => {
    expect(splitDetail('The API key was refused')).toEqual({ head: 'The API key was refused', detail: '' })
    const long = splitDetail('Boat did not answer: HTTPSConnectionPool(host=127.0.0.1, port=9): Max retries exceeded with url: /api/v1/sandboxes')
    expect(long.head).toBe('Boat did not answer')
    expect(long.detail).toContain('HTTPSConnectionPool')
    render(<ErrorState error={new Error('Boat did not answer: HTTPSConnectionPool(host=127.0.0.1): refused')} />)
    expect(screen.getByText('Details')).toBeTruthy()
  })

  it('keeps a plain aside in parentheses in the sentence it belongs to', () => {
    // What sbx-control answers when the runtime is down (app.unexpected): the head once read "the sandbox runtime".
    const down = splitDetail('the sandbox runtime (E2B Embed) failed: ConnectError: Request failed: error sending request for url (http://127.0.0.1:49999/v2/sandboxes)')
    expect(down.head).toBe('the sandbox runtime (E2B Embed) failed')
    expect(down.detail).toMatch(/^ConnectError: Request failed/)
    // A technical parenthesis still starts the detail.
    expect(splitDetail('Could not reach the store (http://10.0.0.5:8200 refused the connection)').head).toBe('Could not reach the store')
    expect(splitDetail('Nothing answered (see http://127.0.0.1:1').head).toBe('Nothing answered')
  })
})
