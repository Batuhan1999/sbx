// How catalog packages read for people: names in words, help as sentences,
// and the catalog grouped by the account or server each package runs on.
import type { CatalogPackage, PackageInput } from '@/api/types'

const UPPER = new Set(['api', 'arn', 'aws', 'byok', 'ca', 'db', 'http', 'https', 'id', 'ip', 'rest', 'sql', 'ssl', 'url', 'usd', 'utc'])

/** An input's name for people: its label, else its name in words (management_key: "Management key"). */
export function inputLabel(name: string, input?: Pick<PackageInput, 'label'>): string {
  if (input?.label) return input.label
  const words = name.split(/[_-]+/).filter(Boolean).map(w => UPPER.has(w.toLowerCase()) ? w.toUpperCase() : w.toLowerCase())
  if (!words.length) return name
  return [words[0][0].toUpperCase() + words[0].slice(1), ...words.slice(1)].join(' ')
}

/** Help text as a sentence: a capital first letter (unless it starts with a value, like a URL) and a full stop. */
export function sentence(text: string): string {
  const t = text.trim()
  if (!t) return ''
  const capital = /^[a-z]+[\s,;]/.test(t) ? t[0].toUpperCase() + t.slice(1) : t
  return /[.!?]$/.test(capital) ? capital : `${capital}.`
}

/** A description's first sentence: what cards and lists show. */
export function summary(text: string): string {
  const first = text.trim().match(/^[\s\S]*?[.!?](?=\s|$)/)
  return (first ? first[0] : text).trim()
}

/** A description's sentences, one by one. */
export function sentences(text: string): string[] {
  return text.trim().split(/(?<=[.!?])\s+(?=[A-Z])/).map(s => s.trim()).filter(Boolean)
}

/** A yes/no input: no choices, and "true" or "false" as its default. */
export const isToggle = (input: PackageInput) => !input.choices?.length && (input.default === 'true' || input.default === 'false')

/** "a" or "an" before a title. */
export const article = (title: string) => /^[aeiou]/i.test(title) ? `an ${title}` : `a ${title}`

/** A name for a new connection: the package name's first word, numbered when it is taken. */
export function suggestKey(packageName: string, taken: string[]): string {
  const base = packageName.split(/[-_]/)[0] || packageName
  let key = base
  for (let n = 2; taken.includes(key); n++) key = `${base}-${n}`
  return key
}

/** One provider in the catalog: an account or server, and the packages that give each sandbox something on it. */
export type Provider = { id: string; title: string; logo: string; tagline: string; account?: CatalogPackage; resources: CatalogPackage[] }

/** The catalog grouped by what packages require: each connection package with the per-sandbox packages that
 * run on it. A per-sandbox package that needs none (or one the catalog lacks) stands alone. */
export function providers(packages: CatalogPackage[]): Provider[] {
  const groups = new Map<string, Provider>()
  for (const account of packages.filter(p => p.kind === 'connection')) {
    groups.set(account.name, { id: account.name, title: account.title, logo: account.logo, tagline: '', account, resources: [] })
  }
  const alone: Provider[] = []
  for (const p of packages.filter(p => p.kind === 'provision')) {
    const home = Object.values(p.requires).map(r => groups.get(r.kind)).find(Boolean)
    if (home) home.resources.push(p)
    else alone.push({ id: p.name, title: p.title, logo: p.logo, tagline: '', resources: [p] })
  }
  return [...groups.values(), ...alone]
    .map(g => ({ ...g, logo: g.logo || g.resources.find(r => r.logo)?.logo || '', tagline: summary(g.resources[0]?.description ?? g.account?.description ?? '') }))
    .sort((a, b) => Number(!a.resources.length) - Number(!b.resources.length) || a.title.localeCompare(b.title))
}

/** The repositories among a used_by list ("repo" or "repo:key"), once each. */
export const repositories = (usedBy: string[]) => [...new Set(usedBy.filter(u => !u.startsWith('sandbox ')).map(u => u.split(':')[0]))]

/** The sandboxes among a connection's used_by list ("sandbox NAME:service"), once each. */
export const sandboxes = (usedBy: string[]) => [...new Set(usedBy.filter(u => u.startsWith('sandbox ')).map(u => u.slice(8).split(':')[0]))]
