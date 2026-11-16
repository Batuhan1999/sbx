// shadcn's theme provider: the choice lives in localStorage and <html> gets
// the class "light" or "dark". "system" follows the OS setting as it changes.
import { createContext, useContext, useLayoutEffect, useState, type ReactNode } from 'react'

export type Theme = 'light' | 'dark' | 'system'
const STORAGE_KEY = 'sbx-theme'

type ThemeState = { theme: Theme; setTheme: (theme: Theme) => void }
const ThemeContext = createContext<ThemeState>({ theme: 'system', setTheme: () => {} })

function stored(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    if (value === 'light' || value === 'dark' || value === 'system') return value
  } catch { /* storage unavailable */ }
  return 'system'
}

const systemDark = () => window.matchMedia?.('(prefers-color-scheme: dark)')

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(stored)

  // Before the first paint, so a dark choice never flashes light.
  useLayoutEffect(() => {
    const root = document.documentElement
    const apply = () => {
      root.classList.remove('light', 'dark')
      root.classList.add(theme === 'system' ? (systemDark()?.matches ? 'dark' : 'light') : theme)
    }
    apply()
    if (theme !== 'system') return
    const media = systemDark()
    media?.addEventListener('change', apply)
    return () => media?.removeEventListener('change', apply)
  }, [theme])

  const setTheme = (next: Theme) => {
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* storage unavailable: this page only */ }
    setThemeState(next)
  }
  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>
}

export const useTheme = () => useContext(ThemeContext)
