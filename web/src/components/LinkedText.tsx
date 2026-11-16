// Text whose web addresses are links: https://… and bare ones such as
// openrouter.ai/settings/keys. Example values are left as text: other schemes
// (postgresql://…) and hosts without a domain (http://host:8123, an IP address).
import type { ReactNode } from 'react'

const ADDRESS = /\bhttps?:\/\/[^\s<>()]*[^\s<>().,;:!?'"]|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\/[^\s<>()]*[^\s<>().,;:!?'"]/gi

/** The web addresses in a text, with where each one starts; example hosts are left out. */
export function webAddresses(text: string): { at: number; text: string; href: string }[] {
  const found = []
  for (const match of text.matchAll(ADDRESS)) {
    const before = text.slice(0, match.index)
    if (/[a-z0-9+.-]:\/\/[^\s]*$/i.test(before) || /@[^\s]*$/.test(before)) continue  // part of another scheme's URL or an email
    const href = /^https?:\/\//i.test(match[0]) ? match[0] : `https://${match[0]}`
    let host = ''
    try { host = new URL(href).hostname } catch { continue }
    if (!/\.[a-z]{2,}$/i.test(host)) continue
    found.push({ at: match.index ?? 0, text: match[0], href })
  }
  return found
}

export function LinkedText({ text }: { text: string }) {
  const parts: ReactNode[] = []
  let last = 0
  for (const a of webAddresses(text)) {
    parts.push(text.slice(last, a.at),
      <a key={a.at} href={a.href} target="_blank" rel="noreferrer" className="break-all text-accent hover:underline">{a.text}</a>)
    last = a.at + a.text.length
  }
  parts.push(text.slice(last))
  return <>{parts}</>
}
