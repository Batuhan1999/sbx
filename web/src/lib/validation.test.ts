import { describe, expect, it } from 'vitest'
import { envNameError, nameFromBranch, repoError, sandboxNameError } from './validation'

describe('sandbox names', () => {
  it('follows the server rule: a letter, then 1–30 of a-z 0-9 -', () => {
    expect(sandboxNameError('feature-login')).toBeUndefined()
    expect(sandboxNameError('a')).toMatch(/2 to 31/)
    expect(sandboxNameError('1abc')).toMatch(/lowercase letter/)
    expect(sandboxNameError('Feature')).toMatch(/lowercase letter/)
    expect(sandboxNameError('feat_login')).toMatch(/hyphens/)
    expect(sandboxNameError('a'.repeat(32))).toMatch(/2 to 31/)
  })

  it('derives a valid name from a branch, or none', () => {
    expect(nameFromBranch('feature/Login-Fix')).toBe('feature-login-fix')
    expect(nameFromBranch('123-hotfix')).toBe('hotfix')
    expect(nameFromBranch('')).toBe('')
    expect(sandboxNameError(nameFromBranch('dependabot/npm_and_yarn/next-15.5.4-and-more-things'))).toBeUndefined()
  })
})

describe('other names', () => {
  it('checks env variable names and repositories', () => {
    expect(envNameError('STRIPE_KEY')).toBeUndefined()
    expect(envNameError('9LIVES')).toBeDefined()
    expect(envNameError('MY-VAR')).toBeDefined()
    expect(repoError('usememos/memos')).toBeUndefined()
    expect(repoError('https://github.com/usememos/memos.git')).toBeUndefined()
    expect(repoError('memos')).toBeDefined()
  })
})
