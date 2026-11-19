import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ApiError, describeError, errorText, request, RESTART } from '@/api/client'
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

describe('request while sbx-control restarts', () => {
  // What the proxy in front of sbx-control answers while it restarts (a new runtime or domain restarts it).
  const PROXY = { status: 502, body: '<html><head><title>502 Bad Gateway</title></head><body><center>nginx</center></body></html>' }
  const waitMs = RESTART.waitMs
  afterEach(() => { RESTART.waitMs = waitMs })

  it('asks a read again until the server answers, so the page never shows the restart', async () => {
    RESTART.waitMs = 0
    let asked = 0
    const calls = stubApi({ 'GET /api/runtimes/available': () => (++asked < 3 ? PROXY : { status: 200, body: [{ kind: 'boat' }] }) })
    expect(await request('GET', '/api/runtimes/available')).toEqual([{ kind: 'boat' }])
    expect(calls).toHaveLength(3)
  })

  it('gives up after a few tries with its own words, never the proxy’s page', async () => {
    RESTART.waitMs = 0
    const calls = stubApi({ 'GET /api/setup': () => PROXY })
    const error = await request<never>('GET', '/api/setup').catch((e: ApiError) => e)
    expect(calls).toHaveLength(RESTART.tries)
    expect(error.gateway).toBe(true)
    expect(error.message).toBe('The server isn’t answering right now; it may be restarting. Try again in a moment.')
    expect(errorText(error)).not.toMatch(/nginx|<html>|Bad Gateway/)
  })

  it('sends a write once: it may have reached the server before it went away', async () => {
    const calls = stubApi({ 'POST /api/runtimes': () => PROXY })
    const error = await request<never>('POST', '/api/runtimes', { kind: 'boat' }).catch((e: ApiError) => e)
    expect(calls).toHaveLength(1)
    expect(error.message).toMatch(/isn’t answering right now/)
  })

  it('takes sbx-control’s own 503 as its answer', async () => {
    const calls = stubApi({ 'GET /api/sandboxes': () => ({ status: 503, body: { detail: 'no free capacity on the sandbox host' } }) })
    const error = await request<never>('GET', '/api/sandboxes').catch((e: ApiError) => e)
    expect(calls).toHaveLength(1)
    expect([error.gateway, error.message]).toEqual([false, 'no free capacity on the sandbox host'])
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
})
