// A release's notes as release.yml publishes them: the annotated tag's
// upgrade steps first, then GitHub's generated list ("## What's Changed",
// "### Breaking changes", "* Title by @someone in .../pull/12",
// "**Full Changelog**: URL").

/** A line of text, or a fenced block kept as written (``` ... ```). In a section, a
 * line that was no bullet is a paragraph (para). Text may hold inline Markdown. */
export type NoteItem = { text: string; code?: boolean; para?: boolean; pr?: { number: string; url: string } }
/** level: the heading's marks (2 for ##); depth: its place under the headings open
 * above it (1 for the outermost, 2 below that), so ## then #### is 1 then 2. generated: in the list GitHub generated
 * (after its "Release notes generated" comment), not a heading the release wrote. */
export type NoteSection = { title: string; level: number; depth: number; breaking: boolean; generated: boolean; items: NoteItem[] }
export type ReleaseNotes = { steps: NoteItem[]; sections: NoteSection[]; compare?: string }

// "Title by @someone in URL" and "@someone made their first contribution in URL".
const PR_LINE = /^(.*?)(?:\s+by\s+@[\w-]+(?:\[bot\])?)?\s+in\s+(https:\/\/github\.com\/\S+\/pull\/(\d+))\s*$/
const COMPARE = /^\*\*Full Changelog\*\*:\s*(https:\/\/\S+)\s*$/
// GitHub writes a "Release notes generated" comment above the list it generates;
// from there to its "Full Changelog" line the headings are its.
const GENERATED_MARK = '\u0001generated'
/** The text without HTML comments, in one pass that knows fenced blocks: a
 * comment in a block is the block's, a block in a comment is the comment's.
 * GitHub's "Release notes generated" comment becomes a line of its own (GENERATED_MARK). */
function withoutComments(text: string): string {
  let out = '', fenced = false
  for (let i = 0; i < text.length;) {
    if (!fenced && text.startsWith('<!--', i)) {
      const end = text.indexOf('-->', i + 4)
      if (/^<!--\s*Release notes generated/.test(text.slice(i, i + 60))) out += `\n${GENERATED_MARK}\n`
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
  let generated = false   // past GitHub's comment: its "What's Changed" and categories
  let blank = true        // the line before was blank (or a block boundary)
  const open: number[] = []   // the levels of the headings above this one
  const items = () => (section ? section.items : notes.steps)
  const add = (item: NoteItem) => { items().push(item); blank = false }
  for (const raw of withoutComments(text.replace(/\r\n/g, '\n')).split('\n')) {
    const line = raw.trim()
    if (line.startsWith('```')) {
      if (fence) { add({ text: fence.join('\n'), code: true }); fence = null; blank = true } else fence = []
      continue
    }
    if (fence) { fence.push(raw.trimEnd()); continue }
    if (!line) { blank = true; continue }
    if (line === GENERATED_MARK) { generated = true; open.length = 0; blank = true; continue }
    const compare = COMPARE.exec(line)
    if (compare) {
      notes.compare = compare[1]
      if (generated) {   // what follows is the release's again: no parent, its own (untitled) section
        generated = false
        open.length = 0
        section = { title: '', level: 0, depth: 0, breaking: false, generated: false, items: [] }
        notes.sections.push(section)
      }
      blank = true
      continue
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      const level = heading[1].length, title = heading[2].trim()
      while (open.length && open[open.length - 1] >= level) open.pop()
      open.push(level)
      section = { title, level, depth: open.length, breaking: /^breaking changes?\b/i.test(title), generated, items: [] }
      notes.sections.push(section)
      blank = true
      continue
    }
    const bullet = /^[*-]\s+(.*)$/.exec(line)
    const last = items().at(-1)
    // A wrapped line belongs to the paragraph or bullet before it, until a blank line;
    // a numbered item ("2. Restart") starts its own.
    if (!bullet && !/^\d+[.)]\s/.test(line) && !blank && last && !last.code && !last.pr) { last.text += ' ' + line; continue }
    if (bullet && section) {
      const pr = PR_LINE.exec(bullet[1])
      add(pr ? { text: pr[1], pr: { number: pr[3], url: pr[2] } } : { text: bullet[1] })
    } else if (section) {
      add({ text: line, para: true })
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
