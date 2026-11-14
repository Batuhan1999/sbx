// Client-side checks that mirror the server's rules, so a form says what is
// wrong before a round trip. The server stays the authority.

export const required = (value: string) => (value.trim() ? undefined : 'Enter a value.')

export const emailError = (value: string) =>
  value.trim().length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim()) ? undefined : 'Enter a valid email address.'

export const passwordError = (value: string) => {
  const length = Array.from(value).length
  return length < 8 ? 'Use at least 8 characters.' : length > 1024 ? 'Use no more than 1024 characters.' : undefined
}

/** Mirrors core.NAME_RE. */
export const sandboxNameError = (value: string) => {
  if (!value) return 'Enter a name.'
  if (!/^[a-z]/.test(value)) return 'Start with a lowercase letter.'
  if (!/^[a-z][a-z0-9-]*$/.test(value)) return 'Use lowercase letters, digits and hyphens.'
  if (value.length < 2 || value.length > 31) return 'Use 2 to 31 characters.'
  return undefined
}

export const nameFromBranch = (branch: string) => {
  const slug = branch.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '').slice(0, 31).replace(/-+$/, '')
  return slug.length >= 2 ? slug : ''
}

/** Mirrors variables.NAME_RE. */
export const envNameError = (value: string) =>
  !value ? 'Enter a name.' : /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? undefined : 'Use letters, digits and underscores; start with a letter or underscore.'

export const keyError = (value: string) =>
  /^[a-z0-9][a-z0-9_-]*$/.test(value) ? undefined : 'Use lowercase letters, digits, underscores or hyphens.'

export const repoError = (value: string) => {
  const v = value.trim().replace(/\/+$/, '').replace(/\.git$/, '').replace(/^https:\/\/github\.com\//, '')
  return /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/.test(v) ? undefined : 'Use owner/repo or a github.com URL.'
}

export const domainError = (value: string) =>
  /^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(value.trim()) ? undefined : 'Enter a domain such as sbx.example.com.'
