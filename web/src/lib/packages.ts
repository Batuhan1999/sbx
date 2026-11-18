// How catalog packages read for people: names in words, help as sentences.
import type { PackageInput } from '@/api/types'

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

/** A yes/no input (the server types an input with a "true" or "false" default boolean). */
export const isToggle = (input: PackageInput) => input.type === 'boolean'

/** "a" or "an" before a title. */
export const article = (title: string) => /^[aeiou]/i.test(title) ? `an ${title}` : `a ${title}`

/** A name for a new connection: the package name's first word, numbered when it is taken. */
export function suggestKey(packageName: string, taken: string[]): string {
  const base = packageName.split(/[-_]/)[0] || packageName
  let key = base
  for (let n = 2; taken.includes(key); n++) key = `${base}-${n}`
  return key
}

/** The repositories among a used_by list ("repo" or "repo:key"), once each. */
export const repositories = (usedBy: string[]) => [...new Set(usedBy.filter(u => !u.startsWith('sandbox ')).map(u => u.split(':')[0]))]

/** The sandboxes among a connection's used_by list ("sandbox NAME:service"), once each. */
export const sandboxes = (usedBy: string[]) => [...new Set(usedBy.filter(u => u.startsWith('sandbox ')).map(u => u.slice(8).split(':')[0]))]
