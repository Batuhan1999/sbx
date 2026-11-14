import { Check, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { Button } from './ui/button'

export async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.className = 'fixed -left-[9999px]'
    document.body.append(area)
    area.select()
    const ok = document.execCommand('copy')
    area.remove()
    return ok
  }
}

export function useCopy() {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  return {
    copied,
    copy: async (text: string) => {
      if (await copyText(text)) {
        setCopied(true)
        window.clearTimeout(timer.current)
        timer.current = window.setTimeout(() => setCopied(false), 1600)
      }
    },
  }
}

export function CopyButton({ text, label = 'Copy', size = 'sm', variant = 'secondary', className }: {
  text: string; label?: string; size?: 'sm' | 'md' | 'icon-sm'; variant?: 'secondary' | 'ghost' | 'primary'; className?: string
}) {
  const { copied, copy } = useCopy()
  return <Button size={size} variant={variant} className={className} onClick={() => copy(text)} aria-label={size === 'icon-sm' ? label : undefined}>
    {copied ? <Check /> : <Copy />}{size !== 'icon-sm' && (copied ? 'Copied' : label)}
    <span className="sr-only" aria-live="polite">{copied ? 'Copied to clipboard' : ''}</span>
  </Button>
}

export function CopyCommand({ command, prompt = true, className, label }: { command: string; prompt?: boolean; className?: string; label?: string }) {
  return <div className={cn('group flex min-w-0 items-center gap-2 rounded-md border border-line bg-sunken py-1 pl-3 pr-1', className)}>
    <code className="min-w-0 grow overflow-x-auto whitespace-pre py-1 text-[12.5px]" aria-label={label}>
      {prompt && <span aria-hidden className="mr-2 select-none text-faint">$</span>}{command}
    </code>
    <CopyButton text={command} size="icon-sm" variant="ghost" label={`Copy ${label ?? 'command'}`} />
  </div>
}
