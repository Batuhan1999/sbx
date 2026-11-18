import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { SecretRequest, SecretRequestItem } from '@/api/types'
import KeysPage from '@/pages/KeysPage'
import { renderApp, stubApi } from './render'

const item = (over: Partial<SecretRequestItem>): SecretRequestItem => ({
  target: 'dns:cloudflare', label: 'Cloudflare DNS token', help_url: 'https://github.com/o/sbx/blob/main/docs/howto/cloudflare.md',
  what: "the Cloudflare API token for this server's certificate", hint: '', reuse: null, reuse_from: null,
  done: false, done_at: null, done_by: null, ...over,
})
const request = (over: Partial<SecretRequest> = {}): SecretRequest => ({
  id: 'req12345', url: 'https://login.sbx.example.com/keys/req12345', state: 'pending', created_by: 'ada@example.com',
  created_at: '2026-11-18T10:00:00Z', expires_at: '2099-01-01T00:00:00Z', note: '', finished_at: null, finished_by: null,
  items: [
    item({ target: 'runtime:boat:BOAT_API_KEY', label: 'Boat API key', what: "Boat's API key", help_url: 'https://github.com/o/sbx/blob/main/docs/howto/boat.md' }),
    item({}),
    item({ target: 'repo:owner/app:BOAT_API_KEY=from:runtime:boat:BOAT_API_KEY', label: 'Boat key for the app', what: "BOAT_API_KEY for owner/app's sandboxes",
      reuse: 'runtime:boat:BOAT_API_KEY', reuse_from: "Boat's API key", help_url: '' }),
  ],
  remaining: 3, ...over,
})
const page = () => renderApp(<Routes><Route path="/app/keys/:id" element={<KeysPage />} /></Routes>, '/app/keys/req12345')

describe('KeysPage', () => {
  it('greets, masks each key, links its guide and shows a reused key without a field', async () => {
    stubApi({ 'GET /api/secret-requests/req12345': request() })
    page()
    expect(await screen.findByRole('heading', { name: 'Your agent needs these keys' })).toBeTruthy()
    const boat = await screen.findByLabelText('Boat API key')
    expect(boat.getAttribute('type')).toBe('password')
    expect(boat.getAttribute('autocomplete')).toBe('off')
    expect(screen.getByLabelText('Cloudflare DNS token').getAttribute('type')).toBe('password')
    const guides = screen.getAllByRole('link', { name: /How do I get this/ })
    expect(guides.map(a => a.getAttribute('href'))).toEqual([
      'https://github.com/o/sbx/blob/main/docs/howto/boat.md', 'https://github.com/o/sbx/blob/main/docs/howto/cloudflare.md'])
    expect(screen.queryByLabelText('Boat key for the app')).toBeNull()
    expect(screen.getByText(/Nothing to paste: the Boat API key you paste here is used for BOAT_API_KEY/)).toBeTruthy()
    expect(screen.getByText(/Asked by ada@example.com/)).toBeTruthy()
  })

  it('submits the pasted keys, clears the saved ones and shows a refused key under its field', async () => {
    const user = userEvent.setup()
    const calls = stubApi({
      'GET /api/secret-requests/req12345': request(),
      'POST /api/secret-requests/req12345/submit': {
        ok: false,
        request: request({ remaining: 1, items: request().items.map((i, n) => (n === 1 ? i : { ...i, done: true, done_at: '2026-11-18T10:05:00Z' })) }),
        results: [
          { target: 'runtime:boat:BOAT_API_KEY', ok: true, error: '' },
          { target: 'dns:cloudflare', ok: false, error: 'Cloudflare refused the token: Invalid API Token' },
          { target: 'repo:owner/app:BOAT_API_KEY=from:runtime:boat:BOAT_API_KEY', ok: true, error: '' },
        ],
      },
    })
    page()
    await user.type(await screen.findByLabelText('Boat API key'), 'test-only-boat')
    await user.type(screen.getByLabelText('Cloudflare DNS token'), 'test-only-dns')
    await user.click(screen.getByRole('button', { name: 'Save keys' }))
    expect(await screen.findByText('Cloudflare refused the token: Invalid API Token')).toBeTruthy()
    const submit = calls.find(c => c.method === 'POST')!
    expect(submit.body).toEqual({ values: { 'runtime:boat:BOAT_API_KEY': 'test-only-boat', 'dns:cloudflare': 'test-only-dns' } })
    expect(screen.queryByLabelText('Boat API key')).toBeNull()
    expect(screen.getAllByText('saved')).toHaveLength(2)
    expect(screen.getByLabelText('Cloudflare DNS token').getAttribute('aria-invalid')).toBe('true')
    expect(document.body.textContent).not.toContain('test-only')
    await user.type(screen.getByLabelText('Cloudflare DNS token'), 'x')   // correcting it clears the error
    await waitFor(() => expect(screen.queryByText(/Invalid API Token/)).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Save keys' }))   // and it can be saved again
    await waitFor(() => expect(calls.filter(c => c.method === 'POST')).toHaveLength(2))
    expect(calls.filter(c => c.method === 'POST')[1].body).toEqual({ values: { 'dns:cloudflare': 'test-only-dnsx' } })
  })

  it('says when every key is in, or the page expired', async () => {
    stubApi({ 'GET /api/secret-requests/req12345': request({ state: 'completed', remaining: 0 }) })
    const first = page()
    expect(await screen.findByText(/All 3 keys are saved/)).toBeTruthy()
    first.unmount()
    stubApi({ 'GET /api/secret-requests/req12345': request({ state: 'expired' }) })
    page()
    expect(await screen.findByText(/This page has expired/)).toBeTruthy()
  })

  it('sends the browser to the sign-in and back when nobody is signed in', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, pathname: '/app/keys/req12345', search: '', hash: '', assign })
    stubApi({ 'GET /api/secret-requests/req12345': () => ({ status: 401, body: { detail: 'sign in: sbx login' } }) })
    page()
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/auth/login?next=%2Fapp%2Fkeys%2Freq12345'))
  })
})
