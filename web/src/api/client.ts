// The one place the web app talks HTTP.

import type { Check } from './types'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(messageOf(detail) || `Request failed (HTTP ${status})`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function messageOf(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message
  // FastAPI validation errors: [{loc, msg, type}]
  if (Array.isArray(detail)) return detail.map(d => (d && typeof d === 'object' && 'msg' in d ? String(d.msg) : String(d))).join('; ')
  return ''
}

export function describeError(error: unknown): { message: string; checks: Check[]; status?: number } {
  if (error instanceof ApiError) {
    const d = error.detail as { checks?: Check[] } | null
    const checks = Array.isArray(d?.checks) ? d!.checks.filter(c => !c.ok) : []
    const message = error.status === 403 && !messageOf(error.detail) ? 'You do not have access to this.'
      : error.status >= 500 && !messageOf(error.detail) ? 'The server could not complete this request. Try again.'
      : error.message
    return { message, checks, status: error.status }
  }
  if (error instanceof TypeError) return { message: 'Could not reach the server. Check your connection and try again.', checks: [] }
  return { message: error instanceof Error ? error.message : String(error), checks: [] }
}

export function errorText(error: unknown): string {
  const { message, checks } = describeError(error)
  return checks.length ? `${message}: ${checks.map(c => `${c.name}: ${c.detail}`).join('; ')}` : message
}

// The first-run setup link carries a one-time token in the URL fragment;
// it stands in for an admin until the setup is complete.
const TOKEN_KEY = 'sbx-setup-token'
export function setSetupToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch { /* storage unavailable: the token lives for this page only */ }
}
export function setupToken(): string | null {
  if (location.pathname.startsWith('/app/setup')) {
    const token = new URLSearchParams(location.hash.slice(1)).get('t')
    if (token && /^[A-Za-z0-9_-]+$/.test(token)) {
      setSetupToken(token)
      history.replaceState(null, '', location.pathname + location.search)
    }
  }
  try { return sessionStorage.getItem(TOKEN_KEY) } catch { return null }
}

/** Pages that work signed out; a 401 there must not bounce to the sign-in. */
const PUBLIC_PATHS = ['/app/setup', '/login', '/app/set-password']

export async function request<T>(method: string, url: string, body?: unknown, accept: 'json' | 'text' = 'json'): Promise<T> {
  const headers: Record<string, string> = { accept: accept === 'json' ? 'application/json' : 'text/plain' }
  if (body !== undefined) headers['content-type'] = 'application/json'
  const token = setupToken()
  if (token) headers.authorization = `Setup ${token}`
  const response = await fetch(url, { method, credentials: 'same-origin', headers, body: body === undefined ? undefined : JSON.stringify(body) })
  const text = await response.text()
  let data: unknown = text
  if (accept === 'json' || !response.ok) {
    try { data = text ? JSON.parse(text) : null } catch { data = text }
  }
  if (!response.ok) {
    if (response.status === 401 && !token && !PUBLIC_PATHS.some(p => location.pathname.startsWith(p))) {
      location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search))
    }
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : data
    throw new ApiError(response.status, detail)
  }
  return data as T
}

export async function postForm<T>(url: string, fields: Record<string, string>): Promise<T> {
  const response = await fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
    body: new URLSearchParams(fields),
  })
  let data: unknown = null
  try { data = await response.json() } catch { /* not JSON */ }
  if (!response.ok) throw new ApiError(response.status, (data as { detail?: unknown } | null)?.detail ?? null)
  return data as T
}

export const enc = encodeURIComponent
export const repoPath = (repo: string) => `/api/repos/${repo.split('/').map(enc).join('/')}`
