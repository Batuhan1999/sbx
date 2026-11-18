import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Updates as UpdatesView, Upgrade } from '@/api/types'
import { UpdateNotice } from '@/app/UpdateNotice'
import { parseNotes } from '@/lib/releaseNotes'
import { Updates } from '@/pages/settings/Updates'
import { renderApp, stubApi } from './render'

const GENERATED = `Move the setting first.
<!-- Release notes generated using configuration in .github/release.yml at v0.2.0 -->

## What's Changed
### Breaking changes
* Rename a setting by @someone in https://github.com/o/sbx/pull/12
### Changes
* Faster previews by @someone in https://github.com/o/sbx/pull/14

## New Contributors
* @someone made their first contribution in https://github.com/o/sbx/pull/2

**Full Changelog**: https://github.com/o/sbx/compare/v0.1.0...v0.2.0`

const release = (tag: string, notes: string, breaking = false) => ({
  tag, url: `https://github.com/o/sbx/releases/tag/${tag}`, notes, breaking, prerelease: false, published_at: '2026-11-15T12:00:00Z',
})
const view = (over: Partial<UpdatesView>): UpdatesView => ({
  installed: 'v0.1.0', check: true, checked_at: '2026-11-15T12:00:00Z', check_error: '', available: null, upgrade: null, ...over,
})
const running: Upgrade = {
  tag: 'v0.2.0', from: 'v0.1.0', state: 'running', log: '/var/log/sbx-upgrade.log', progress: 0.37, log_tail: ['==> E2B Embed'],
  steps: [{ title: 'Download sbx-v0.2.0.tar.gz and check its .sha256', state: 'done', detail: '1.1 of 1.1 MB' },
    { title: 'Preflight', state: 'done', detail: '' }, { title: 'E2B Embed', state: 'running', detail: '' }],
}

describe('parseNotes', () => {
  it('splits upgrade steps, sections and pull requests', () => {
    const notes = parseNotes(GENERATED)
    expect(notes.steps).toEqual([{ text: 'Move the setting first.' }])
    expect(notes.sections.map(s => [s.title, s.breaking])).toEqual([['Breaking changes', true], ['Changes', false], ['New Contributors', false]])
    expect(notes.sections[2].items).toEqual([{ text: '@someone made their first contribution', pr: { number: '2', url: 'https://github.com/o/sbx/pull/2' } }])
    expect(notes.sections[0].items).toEqual([{ text: 'Rename a setting', pr: { number: '12', url: 'https://github.com/o/sbx/pull/12' } }])
    expect(notes.compare).toBe('https://github.com/o/sbx/compare/v0.1.0...v0.2.0')
  })

  it('keeps a fenced block as written', () => {
    const notes = parseNotes('Set this first:<!-- a note -->\n```yaml\nupdates:\n  check: false  # <!-- kept -->\n```\n## Changes\n* x')
    expect(notes.steps).toEqual([{ text: 'Set this first:' }, { text: 'updates:\n  check: false  # <!-- kept -->', code: true }])
    const hidden = parseNotes('Shown.\n<!-- an example:\n```\nnot shown\n```\n-->\nAlso shown.')
    expect(hidden.steps).toEqual([{ text: 'Shown.' }, { text: 'Also shown.' }])
  })

  it('reads GitHub\'s marker only outside code, ends its list at Full Changelog and keeps numbered steps apart', () => {
    const notes = parseNotes([
      '1. Back up the state.', '2. Restart the service.', '',
      '```', '<!-- Release notes generated example -->', '```', '',
      '<!-- Release notes generated using configuration in .github/release.yml at v0.2.0 -->',
      "## What's Changed", '### Changes', '* Faster previews by @someone in https://github.com/o/sbx/pull/14', '',
      '**Full Changelog**: https://github.com/o/sbx/compare/v0.1.0...v0.2.0', '- Restart after upgrading.', '',
      '### Rollback', '- Only if needed.', '#### Steps', '- Stop first.',
    ].join('\n'))
    expect(notes.steps).toEqual([{ text: '1. Back up the state.' }, { text: '2. Restart the service.' },
      { text: '<!-- Release notes generated example -->', code: true }])
    expect(notes.sections.map(s => [s.title, s.generated, s.depth])).toEqual([['Changes', true, 2], ['', false, 0], ['Rollback', false, 1], ['Steps', false, 2]])
    expect(notes.sections[0].items).toHaveLength(1)
    expect(notes.sections[1].items).toEqual([{ text: 'Restart after upgrading.' }])
  })

  it('keeps the release\'s own headings, paragraphs and groups apart from the generated list', () => {
    const notes = parseNotes([
      'Intro line that the author', 'wrapped.', '',
      '## Highlights', '', '### Runtimes', '- **Boat** runs', '  `sbx up`.', '',
      "## What's Changed", '### Written by the release', '- An authored item.', '',
      '## Upgrading', '- No steps.', '', 'Full docs: docs/reference.md', '',
      '<!-- Release notes generated using configuration in .github/release.yml at v0.2.0 -->', '',
      "## What's Changed", '### Changes', '* Faster previews by @someone in https://github.com/o/sbx/pull/14',
    ].join('\n'))
    expect(notes.steps).toEqual([{ text: 'Intro line that the author wrapped.' }])
    expect(notes.sections.map(s => [s.title, s.level, s.generated])).toEqual([
      ['Highlights', 2, false], ['Runtimes', 3, false], ["What's Changed", 2, false], ['Written by the release', 3, false],
      ['Upgrading', 2, false], ['Changes', 3, true]])
    expect(notes.sections[1].items).toEqual([{ text: '**Boat** runs `sbx up`.' }])
    expect(notes.sections[4].items).toEqual([{ text: 'No steps.' }, { text: 'Full docs: docs/reference.md', para: true }])
  })
})

describe('Updates page', () => {
  it('renders inline Markdown in the notes as formatting, not characters', async () => {
    const notes = 'Read this.\n\n## Highlights\n- **Boat** runs \`sbx up\`, see [the docs](https://example.com/docs).\n\nA paragraph.\n\n'
      + '- [Run \`sbx up\`](https://example.com/setup(v2)) first.\n\n#### Deeper\n- x\n\n##### Deepest\n- y'
    stubApi({ 'GET /api/updates': view({ available: { latest: 'v0.2.0', url: 'u', breaking: false, releases: [release('v0.2.0', notes)] } }) })
    renderApp(<Updates />)
    const card = await screen.findByRole('region', { name: 'Release v0.2.0' })
    expect(within(card).getByText('Boat').tagName).toBe('STRONG')
    expect(within(card).getAllByText('sbx up').map(e => e.tagName)).toEqual(['CODE', 'CODE'])
    expect(within(card).getByRole('link', { name: 'the docs' })).toHaveProperty('href', 'https://example.com/docs')
    expect(card.textContent).not.toContain('**')
    const run = within(card).getByRole('link', { name: 'Run sbx up' })
    expect(run).toHaveProperty('href', 'https://example.com/setup(v2)')
    expect(run.querySelector('code')?.textContent).toBe('sbx up')
    expect(within(card).getByText('A paragraph.').closest('li')).toBeNull()
    expect(within(card).getAllByRole('list')).toHaveLength(4)   // bullet, paragraph, bullet; then Deeper's and Deepest's
    expect(within(card).getByRole('heading', { name: 'Deeper' }).tagName).toBe('H5')   // ## then ####: no level skipped
    expect(within(card).getByRole('heading', { name: 'Deepest' }).tagName).toBe('H6')
  })

  it('lists what changed, asks for the breaking changes to be read, then posts the tag', async () => {
    const available = { latest: 'v0.2.0', url: 'u', breaking: true, releases: [release('v0.2.0', GENERATED, true)] }
    const calls = stubApi({
      'GET /api/updates': view({ available }),
      'POST /api/updates': view({ upgrade: running }),
    })
    renderApp(<Updates />)
    const card = await screen.findByRole('region', { name: 'Release v0.2.0' })
    expect(card.textContent).toContain('Move the setting first.')
    expect(within(card).getByRole('link', { name: '#12' }).getAttribute('href')).toBe('https://github.com/o/sbx/pull/12')
    const install = screen.getByRole('button', { name: 'Update to v0.2.0' })
    expect(install).toHaveProperty('disabled', true)
    await userEvent.click(screen.getByRole('checkbox', { name: /read the breaking changes/ }))
    await userEvent.click(install)
    await userEvent.click(screen.getByRole('button', { name: 'Update now' }))
    expect(calls.find(c => c.method === 'POST')).toMatchObject({ url: '/api/updates', body: { tag: 'v0.2.0' } })
    const steps = await screen.findByRole('list', { name: 'Steps' })
    expect(steps.textContent).toContain('1.1 of 1.1 MB')
    expect(screen.getByRole('progressbar', { name: 'Updating to v0.2.0' }).getAttribute('aria-valuenow')).toBe('37')
    expect(screen.queryByRole('region', { name: 'Release v0.2.0' })).toBeNull()   // no second update while one runs
  })

  it('asks again when a newer breaking release replaces the one acknowledged', async () => {
    const offer = (tag: string) => view({ available: { latest: tag, url: 'u', breaking: true, releases: [release(tag, GENERATED, true)] } })
    stubApi({ 'GET /api/updates': offer('v0.2.0'), 'POST /api/updates/check': offer('v0.3.0') })
    renderApp(<Updates />)
    await userEvent.click(await screen.findByRole('checkbox', { name: /read the breaking changes/ }))
    expect(screen.getByRole('button', { name: 'Update to v0.2.0' })).toHaveProperty('disabled', false)
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }))
    expect(await screen.findByRole('button', { name: 'Update to v0.3.0' })).toHaveProperty('disabled', true)
    expect(screen.getByRole('checkbox', { name: /read the breaking changes/ }).getAttribute('aria-checked')).toBe('false')
  })

  it('says why an upgrade failed, and how to go back while the new release runs', async () => {
    stubApi({ 'GET /api/updates': view({ installed: 'v0.2.0', upgrade: { ...running, state: 'failed', error: 'Embed would be recreated',
      rollback: 'sh /var/lib/sbx/upgrade-backups/1/rollback.sh', steps: [{ ...running.steps![2], state: 'failed' }] } }) })
    renderApp(<Updates />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Embed would be recreated')
    expect(alert.textContent).toContain('rollback.sh')
  })

  it('checks again on request, and is up to date only once GitHub answered', async () => {
    const calls = stubApi({ 'GET /api/updates': view({ checked_at: '' }), 'POST /api/updates/check': view({ checked_at: new Date().toISOString() }) })
    renderApp(<Updates />)
    expect((await screen.findByText('Not checked yet'))).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url === '/api/updates/check')).toBe(true))
    expect(await screen.findByText('Up to date')).toBeTruthy()
  })
})

describe('UpdateNotice', () => {
  it('points to the Updates page and shows an upgrade’s progress', async () => {
    stubApi({ 'GET /api/updates': view({ available: { latest: 'v0.2.0', url: 'u', breaking: false, releases: [release('v0.2.0', '')] } }) })
    renderApp(<UpdateNotice />)
    expect((await screen.findByRole('link', { name: /v0.2.0 available/ })).getAttribute('href')).toBe('/app/settings/updates')
  })

  it('shows the running upgrade with its percentage', async () => {
    stubApi({ 'GET /api/updates': view({ upgrade: running }) })
    renderApp(<UpdateNotice />)
    expect((await screen.findByRole('status')).textContent).toContain('37%')
  })

  it('shows nothing when this is the newest release', async () => {
    const calls = stubApi({ 'GET /api/updates': view({}) })
    const { container } = renderApp(<UpdateNotice />)
    await waitFor(() => expect(calls.length).toBe(1))
    expect(container.textContent).toBe('')
  })
})
