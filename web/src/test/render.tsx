import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { vi } from 'vitest'

export type Call = { method: string; url: string; body: unknown }

/** Stubs fetch with answers by "METHOD /path" (prefix match); records every call. */
export function stubApi(answers: Record<string, unknown | ((body: unknown) => { status: number; body: unknown })>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET'
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
    calls.push({ method, url, body })
    const key = Object.keys(answers).find(k => `${method} ${url}`.startsWith(k))
    const answer = key ? answers[key] : { status: 404, body: { detail: `no stub for ${method} ${url}` } }
    const { status, body: data } = typeof answer === 'function' ? answer(body) : key ? { status: 200, body: answer } : answer as { status: number; body: unknown }
    return { ok: status < 400, status, text: async () => (typeof data === 'string' ? data : JSON.stringify(data)), json: async () => data }
  }))
  return calls
}

export function renderApp(ui: ReactNode, path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="*" element={ui} /></Routes>
    </MemoryRouter>
  </QueryClientProvider>)
}

export const ME = { email: 'ada@example.com', admin: true, login: 'password', domain: 'sbx.example.com', base_url: 'https://sbx.example.com', setup_complete: true, version: 'dev', repos: ['usememos/memos'], agents: {} }
