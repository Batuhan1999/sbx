import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import type { Check } from '@/api/types'
import { ErrorState } from '@/components/states'
import { Checks } from '@/pages/extensions/ConnectionCard'

const page = 'https://github.com/o/sbx/blob/main/docs/howto/cloudflare.md#dns-records'
const checks: Check[] = [
  { name: '*.sbx.example.com resolves', ok: false, detail: 'no address', fix: 'a wildcard A record', help_url: page },
  { name: 'certificate', ok: true, detail: 'valid', fix: '', help_url: 'https://github.com/o/sbx/blob/main/docs/howto/cloudflare.md#api-token' },
]

describe('how-to links', () => {
  it('links a failed check to its how-to page, and not a passed one', () => {
    render(<Checks checks={checks} />)
    const links = screen.getAllByRole('link', { name: /How to set it up/ })
    expect(links.map(l => l.getAttribute('href'))).toEqual([page])
    expect(links[0].getAttribute('target')).toBe('_blank')
  })

  it('links the failed checks a refusal carries', () => {
    render(<ErrorState error={new ApiError(400, { message: 'Check the DNS records', checks })} />)
    expect(screen.getByRole('link', { name: /How to set it up/ }).getAttribute('href')).toBe(page)
  })
})
