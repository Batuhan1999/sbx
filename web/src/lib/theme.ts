// ?theme= in the URL is how screenshots pick one.
export type ThemeChoice = 'light' | 'dark' | 'system'
const KEY = 'sbx-theme'

export function themeChoice(): ThemeChoice {
  const param = new URLSearchParams(location.search).get('theme')
  if (param === 'light' || param === 'dark' || param === 'system') {
    save(param)
    return param
  }
  try {
    const stored = localStorage.getItem(KEY)
    if (stored === 'light' || stored === 'dark') return stored
  } catch { /* storage unavailable */ }
  return 'system'
}

function save(choice: ThemeChoice) {
  try {
    if (choice === 'system') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, choice)
  } catch { /* storage unavailable */ }
}

const media = () => window.matchMedia?.('(prefers-color-scheme: dark)')

export function applyTheme(choice: ThemeChoice) {
  save(choice)
  const dark = choice === 'dark' || (choice === 'system' && !!media()?.matches)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.dataset.themeChoice = choice
}

export function initTheme() {
  applyTheme(themeChoice())
  media()?.addEventListener('change', () => {
    if (document.documentElement.dataset.themeChoice === 'system') applyTheme('system')
  })
}
