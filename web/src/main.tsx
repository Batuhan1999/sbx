import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router'
import { Toaster } from './components/ui/sonner'
import { router } from './app/router'
import { ThemeProvider } from './components/ThemeProvider'
import './styles.css'

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, retry: (count, error) => count < 1 && !(error && 'status' in error && Number(error.status) < 500) } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
