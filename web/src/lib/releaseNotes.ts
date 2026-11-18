// A release's notes as release.yml publishes them: the annotated tag's
// upgrade steps first, then GitHub's generated list ("## What's Changed",
// "### Breaking changes", "* Title by @someone in .../pull/12",
// "**Full Changelog**: URL").

/** A line of text, or a fenced block kept as written (``` ... ```). In a section, a
 * line that was no bullet is a paragraph (para). Text may hold inline Markdown. */
export type NoteItem = { text: string; code?: boolean; para?: boolean; pr?: { number: string; url: string } }
/** level: the heading's depth (2 for ##). generated: one of GitHub's categories
 * (under "What's Changed" or "New Contributors"), not a heading the release wrote. */
export type NoteSection = { title: string; level: number; breaking: boolean; generated: boolean; items: NoteItem[] }
export type ReleaseNotes = { steps: NoteItem[]; sections: NoteSection[]; compare?: string }

// "Title by @someone in URL" and "@someone made their first contribution in URL".
const PR_LINE = /^(.*?)(?:\s+by\s+@[\w-]+(?:\[bot\])?)?\s+in\s+(https:\/\/github\.com\/\S+\/pull\/(\d+))\s*$/
const COMPARE = /^\*\*Full Changelog\*\*:\s*(https:\/\/\S+)\s*$/
/** The text without HTML comments (the generator's), in one pass that
 * knows fenced blocks: a comment in a block is the block's, a block in a
 * comment is the comment's. */
function withoutComments(text: string): string {
  let out = '', fenced = false
  for (let i = 0; i < text.length;) {
    if (!fenced && text.startsWith('<!--', i)) {
      const end = text.indexOf('-->', i + 4)
      i = end < 0 ? text.length : end + 3
      continue
    }
    if ((i === 0 || text[i - 1] === '\n') && /^[ \t]*```/.test(text.slice(i, i + 80))) fenced = !fenced
    out += text[i++]
  }
  return out
}

export function parseNotes(text: string): ReleaseNotes {
  const notes: ReleaseNotes = { steps: [], sections: [] }
  let section: NoteSection | null = null
  let fence: string[] | null = null
  let generated = false   // under GitHub's "What's Changed" / "New Contributors"
  let generatedLevel = 0
  const add = (item: NoteItem) => (section ? section.items : notes.steps).push(item)
  for (const raw of withoutComments(text.replace(/\r\n/g, '\n')).split('\n')) {
    const line = raw.trim()
    if (line.startsWith('```')) {
      if (fence) { add({ text: fence.join('\n'), code: true }); fence = null } else fence = []
      continue
    }
    if (fence) { fence.push(raw.trimEnd()); continue }
    if (!line) continue
    const compare = COMPARE.exec(line)
    if (compare) { notes.compare = compare[1]; continue }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      const level = heading[1].length, title = heading[2].trim()
      if (generated && level <= generatedLevel) generated = false
      const opens = /^(what's changed|new contributors)$/i.test(title)
      if (opens) { generated = true; generatedLevel = level }
      section = { title, level, breaking: /^breaking changes?\b/i.test(title), generated, items: [] }
      notes.sections.push(section)
      continue
    }
    const bullet = /^[*-]\s+(.*)$/.exec(line)
    if (bullet && section) {
      const pr = PR_LINE.exec(bullet[1])
      section.items.push(pr ? { text: pr[1], pr: { number: pr[3], url: pr[2] } } : { text: bullet[1] })
    } else if (section) {
      section.items.push({ text: line, para: true })
    } else {
      add({ text: bullet ? bullet[1] : line })
    }
  }
  if (fence) add({ text: fence.join('\n'), code: true })   // a block left open runs to the end
  // An empty heading stays when deeper sections with items follow it (a group, like
  // "Highlights"); GitHub's "What's Changed" only holds the categories below it.
  notes.sections = notes.sections.filter((s, i, all) => {
    if (s.items.length) return true
    if (s.generated) return false
    for (const next of all.slice(i + 1)) {
      if (next.level <= s.level) return false
      if (next.items.length) return true
    }
    return false
  })
  return notes
}
