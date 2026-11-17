// shadcn/ui's Sonner Toaster (MIT, see LICENSE.md), on this app's tokens and theme.
import { Toaster as Sonner } from 'sonner'
import { useTheme } from '../ThemeProvider'

export function Toaster() {
  const { theme } = useTheme()
  return <Sonner theme={theme} position="bottom-right" closeButton
    style={{
      '--normal-bg': 'var(--panel)', '--normal-text': 'var(--ink)', '--normal-border': 'var(--line)',
      '--success-bg': 'var(--panel)', '--success-text': 'var(--ink)', '--success-border': 'var(--line)',
      '--error-bg': 'var(--panel)', '--error-text': 'var(--bad)', '--error-border': 'var(--line)',
      '--border-radius': '10px',
    } as React.CSSProperties}
    toastOptions={{ classNames: { toast: 'text-[13px] shadow-lg', description: 'text-muted', icon: '[[data-type=success]_&]:text-ok' } }} />
}
