import { useQueryClient } from '@tanstack/react-query'
import { Bot, Check, Copy, FileText } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAgents } from '@/api/queries'
import { AgentSetupCommands } from '@/components/AgentSetup'
import { ErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'

async function toClipboard(text: Promise<string>): Promise<boolean> {
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (!clipboard) return false
  try {
    // A ClipboardItem takes the text while it is still loading, so the click's
    // permission holds in browsers that expire it during the fetch (Safari).
    if (typeof ClipboardItem !== 'undefined' && clipboard.write) {
      await clipboard.write([new ClipboardItem({ 'text/plain': text.then(t => new Blob([t], { type: 'text/plain' })) })])
    } else {
      await clipboard.writeText(await text)
    }
    return true
  } catch {
    return false
  }
}

export function CopyPromptButton({ repo }: { repo: string }) {
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [shown, setShown] = useState<string | null>(null)
  const timer = useRef<number>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const brief = () => client.fetchQuery({ queryKey: keys.brief(repo), queryFn: () => api.repos.setupBrief(repo), staleTime: 60_000 })

  async function copy() {
    setError(null)
    setBusy(true)
    const text = brief()
    try {
      const ok = await toClipboard(text)
      const value = await text
      if (ok) {
        setCopied(true)
        window.clearTimeout(timer.current)
        timer.current = window.setTimeout(() => setCopied(false), 1600)
      } else {
        setShown(value)
      }
    } catch (failure) {
      setError(failure)
    } finally {
      setBusy(false)
    }
  }

  async function view() {
    setError(null)
    try { setShown(await brief()) } catch (failure) { setError(failure) }
  }

  return <>
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="primary" busy={busy} onClick={copy}>{copied ? <Check /> : <Copy />}{copied ? 'Copied' : 'Copy prompt'}</Button>
      <Button variant="ghost" onClick={view}><FileText />View</Button>
      <span className="sr-only" aria-live="polite">{copied ? 'Copied to clipboard' : ''}</span>
    </div>
    <ErrorState error={error} compact className="mt-2" />
    <Dialog open={shown !== null} onOpenChange={open => { if (!open) setShown(null) }} wide title="Setup prompt">
      <textarea readOnly value={shown ?? ''} aria-label="Setup prompt" rows={18} spellCheck={false}
        onFocus={event => event.currentTarget.select()}
        className="w-full resize-y rounded-md border border-line bg-sunken p-3 font-mono text-[11.5px] leading-relaxed" />
    </Dialog>
  </>
}

export function AgentHandoff({ repo, configured }: { repo: string; configured: boolean }) {
  const agents = useAgents()
  return <section aria-labelledby="handoff-title" className="rounded-lg border border-accent/25 bg-accent-soft/40 p-4 sm:p-5">
    <div className="flex items-start gap-3">
      <span className="grid size-8 shrink-0 place-items-center rounded-md bg-accent text-accent-ink"><Bot className="size-4" /></span>
      <div className="min-w-0 grow">
        <h2 id="handoff-title" className="font-semibold">{configured ? 'Hand the remaining work to your coding agent' : 'Give this to your coding agent'}</h2>
        <ol className="mt-4 space-y-3 text-[13px]">
          <li>
            <p className="mb-1.5"><span className="mr-1.5 font-medium">1.</span>Paste the setup prompt into any coding agent working in a checkout of <span className="font-medium">{repo}</span>:</p>
            <CopyPromptButton repo={repo} />
          </li>
          <li>
            <p className="mb-1.5"><span className="mr-1.5 font-medium">2.</span>Or, with the sbx CLI, run in the checkout:</p>
            <AgentSetupCommands agents={agents} className="max-w-lg bg-panel" />
            <p className="mt-1.5 text-[12px] text-muted">No CLI yet? <Link to="/app/cli" className="text-accent hover:underline">Install it</Link>.</p>
          </li>
        </ol>
      </div>
    </div>
  </section>
}
