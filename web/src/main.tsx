import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setNonce } from 'get-nonce'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router'
import { router } from './app/router'
import { initTheme } from './lib/theme'
import './styles.css'

// The server's CSP lets in only inline <style> with this page's nonce; Radix's scroll lock adds one.
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content
if (nonce) setNonce(nonce)

initTheme()

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: (count, error) => count < 1 && !(error && 'status' in error && Number(error.status) < 500) } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
