// The one place the web app talks HTTP.

import type { Check } from './types'

/** What the page says when the proxy in front of sbx-control answers for it: the server is restarting (a new
 * runtime or domain restarts it) or down, and the proxy's own error page is not for people. */
const NOT_ANSWERING = 'The server isn’t answering right now; it may be restarting. Try again in a moment.'

export class ApiError extends Error {
  status: number
  detail: unknown
  /** The proxy answered, not sbx-control (a 502, 503 or 504 that is not JSON). */
  gateway: boolean
  constructor(status: number, detail: unknown, gateway = false) {
    super(gateway ? NOT_ANSWERING : messageOf(detail) || `Request failed (HTTP ${status})`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.gateway = gateway
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

/** Pages that work signed out; a 401 there must not bounce to the sign-in. */
const PUBLIC_PATHS = ['/onboarding', '/login', '/app/set-password']

/** How often, and how far apart, a read is asked again while the proxy answers for a restarting sbx-control
 * (a restart takes a few seconds): the page rides through it instead of showing an error. */
export const RESTART = { tries: 6, waitMs: 1_500 }

export async function request<T>(method: string, url: string, body?: unknown, accept: 'json' | 'text' = 'json'): Promise<T> {
  const headers: Record<string, string> = { accept: accept === 'json' ? 'application/json' : 'text/plain' }
  if (body !== undefined) headers['content-type'] = 'application/json'
  let response: Response, text: string, data: unknown, gateway: boolean
  for (let tried = 1; ; tried++) {
    response = await fetch(url, { method, credentials: 'same-origin', headers, body: body === undefined ? undefined : JSON.stringify(body) })
    text = await response.text()
    data = text
    let json = true
    if (accept === 'json' || !response.ok) {
      try { data = text ? JSON.parse(text) : null } catch { data = text; json = false }
    }
    gateway = !json && [502, 503, 504].includes(response.status)
    // Only a read is asked again: a write may have reached the server before it went away.
    if (!gateway || method !== 'GET' || tried >= RESTART.tries) break
    await new Promise(resolve => setTimeout(resolve, RESTART.waitMs))
  }
  if (gateway) throw new ApiError(response.status, null, true)
  if (!response.ok) {
    if (response.status === 401 && !PUBLIC_PATHS.some(p => location.pathname.startsWith(p))) {
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
