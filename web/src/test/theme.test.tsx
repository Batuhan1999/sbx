import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ThemeProvider, useTheme } from '@/components/ThemeProvider'

function Switch() {
  const { theme, setTheme } = useTheme()
  return <><span>{theme}</span><button onClick={() => setTheme('dark')}>Dark</button></>
}

/** A stand-in for the OS setting that tests can flip. */
function stubSystem(dark: boolean) {
  const listeners = new Set<() => void>()
  const media = { matches: dark, addEventListener: (_: string, f: () => void) => listeners.add(f), removeEventListener: (_: string, f: () => void) => listeners.delete(f) }
  vi.stubGlobal('matchMedia', () => media)
  return (next: boolean) => { media.matches = next; listeners.forEach(f => f()) }
}

const html = () => document.documentElement.classList

describe('ThemeProvider', () => {
  beforeEach(() => {
    localStorage.clear()
    html().remove('light', 'dark')
    history.replaceState(null, '', '/')
  })

  it('applies the choice saved in localStorage', () => {
    stubSystem(false)
    localStorage.setItem('sbx-theme', 'dark')
    render(<ThemeProvider><Switch /></ThemeProvider>)
    expect(html().contains('dark')).toBe(true)
    expect(screen.getByText('dark')).toBeTruthy()
  })

  it('ignores a theme in the URL', () => {
    stubSystem(false)
    history.replaceState(null, '', '/?theme=dark')
    render(<ThemeProvider><Switch /></ThemeProvider>)
    expect(html().contains('light')).toBe(true)
    expect(localStorage.getItem('sbx-theme')).toBeNull()
  })

  it('saves a new choice and switches the class', async () => {
    stubSystem(false)
    render(<ThemeProvider><Switch /></ThemeProvider>)
    await userEvent.click(screen.getByRole('button', { name: 'Dark' }))
    expect(localStorage.getItem('sbx-theme')).toBe('dark')
    expect(html().contains('dark')).toBe(true)
    expect(html().contains('light')).toBe(false)
  })

  it('follows the system setting while the choice is system', () => {
    const setSystem = stubSystem(false)
    render(<ThemeProvider><Switch /></ThemeProvider>)
    expect(html().contains('light')).toBe(true)
    act(() => setSystem(true))
    expect(html().contains('dark')).toBe(true)
  })
})
