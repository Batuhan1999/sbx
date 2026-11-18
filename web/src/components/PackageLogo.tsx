import { ArrowUpRight, Package } from 'lucide-react'
import type { PackageInput } from '@/api/types'
import type { FieldControls } from '@/components/form'
import { inputLabel, isToggle, sentence, summary } from '@/lib/packages'
import { cn } from '@/lib/utils'
import { emailError, numberError, urlError } from '@/lib/validation'
import { LinkedText } from '@/components/LinkedText'

/** A catalog package's own logo (a data: URL of the SVG its package.yaml names), else the
 * generic package icon. An <img> runs no script in the SVG. Providers draw their marks for
 * light backgrounds, so the tile stays light in the dark theme too. */
export function PackageLogo({ logo, large, className }: { logo?: string; large?: boolean; className?: string }) {
  return <span aria-hidden className={cn('grid shrink-0 place-items-center rounded-md border',
    large ? 'size-8' : 'size-6',
    logo ? 'border-line bg-white dark:border-transparent dark:bg-ink' : 'border-line bg-sunken text-muted', className)}>
    {logo ? <img src={logo} alt="" className={cn('object-contain', large ? 'size-5' : 'size-4')} />
      : <Package className={large ? 'size-4' : 'size-3.5'} />}
  </span>
}

/** A list of packages to choose one from: logo, title and what it gives. */
export function PackagePicker({ packages, onChoose }: {
  packages: { name: string; title: string; description: string; logo?: string }[]; onChoose: (name: string) => void
}) {
  return <ul className="-mx-2 space-y-0.5">
    {packages.map(p => <li key={p.name}><button type="button" onClick={() => onChoose(p.name)} className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-hover">
      <PackageLogo logo={p.logo} large />
      <span className="min-w-0">
        <span className="block font-medium">{p.title}</span>
        <span className="mt-0.5 line-clamp-2 text-[12.5px] text-muted">{summary(p.description)}</span>
      </span>
    </button></li>)}
  </ul>
}

/** A link to the provider's own guide, for getting the key or account a package asks for. */
export function DocsLink({ href, children = 'Setup guide' }: { href?: string; children?: React.ReactNode }) {
  if (!href) return null
  return <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-[12.5px] text-accent hover:underline">{children}<ArrowUpRight className="size-3" /></a>
}

/** The chosen package above its form: logo, title, what it gives and its guide. */
export function PackageHeading({ logo, title, description, docs, docsLabel }: { logo?: string; title: string; description?: string; docs?: string; docsLabel?: string }) {
  return <div className="flex items-start gap-3">
    <PackageLogo logo={logo} large />
    <div className="min-w-0">
      <p className="font-medium">{title}</p>
      {description && <p className="text-[12.5px] text-muted"><LinkedText text={summary(description)} /></p>}
      <DocsLink href={docs}>{docsLabel}</DocsLink>
    </div>
  </div>
}

/** One package input in a form, the way people read it: its label, a list for its choices, a checkbox
 * for yes or no, a hidden field for a secret. */
export function PackageInputControl({ field, name, input, placeholder, note }: {
  field: FieldControls; name: string; input: PackageInput; placeholder?: string; note?: string
}) {
  const label = inputLabel(name, input)
  const text = [input.help && sentence(input.help), note].filter(Boolean).join(' ')
  const help = text ? <LinkedText text={text} /> : undefined
  if (input.choices?.length) {
    const blank = !input.choices.some(c => c.value === '') && !input.default
    return <field.SelectField label={label} help={help}>
      {blank && <option value="">{input.required ? 'Choose…' : 'Not set'}</option>}
      {input.choices.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
    </field.SelectField>
  }
  if (isToggle(input)) return <field.ToggleField label={label} help={help} />
  const common = { label, help, optional: !input.required, placeholder, autoComplete: 'off', spellCheck: false }
  if (input.type === 'number' || input.type === 'integer') return <field.NumberField {...common} min={input.min} max={input.max}
    step={input.type === 'integer' ? 1 : 'any'} />
  return <field.TextField {...common} type={input.secret ? 'password' : input.type === 'email' ? 'email' : input.type === 'url' ? 'url' : 'text'} />
}

/** What is wrong with a package input's value before it is sent (the server checks again). */
export function packageInputError(input: PackageInput, value: string): string | undefined {
  const v = (value ?? '').trim()
  if (!v) return undefined
  if (input.type === 'number' || input.type === 'integer') return numberError(v, { min: input.min, max: input.max, integer: input.type === 'integer' })
  if (input.type === 'email') return emailError(v)
  if (input.type === 'url') return urlError(v)
  return undefined
}
