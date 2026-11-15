import { Package } from 'lucide-react'
import { cn } from '@/lib/utils'

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

/** A list of packages to choose one from: logo, title, name and description. */
export function PackagePicker({ packages, onChoose }: {
  packages: { name: string; title: string; description: string; logo?: string }[]; onChoose: (name: string) => void
}) {
  return <ul className="-mx-2 space-y-0.5">
    {packages.map(p => <li key={p.name}><button type="button" onClick={() => onChoose(p.name)} className="flex w-full items-start gap-3 rounded-md px-2 py-2 text-left hover:bg-hover">
      <PackageLogo logo={p.logo} className="mt-0.5" />
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-x-2"><span className="font-medium">{p.title}</span><span className="font-mono text-[11.5px] text-faint">{p.name}</span></span>
        <span className="mt-0.5 line-clamp-2 text-[12.5px] text-muted">{p.description}</span>
      </span>
    </button></li>)}
  </ul>
}

/** The chosen package above its form: logo and title. */
export function PackageHeading({ logo, title }: { logo?: string; title: string }) {
  return <div className="flex items-center gap-2.5"><PackageLogo logo={logo} /><span className="font-medium">{title}</span></div>
}
