import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'
import { X } from 'lucide-react'

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'

export function Button({
  variant = 'secondary',
  size = 'md',
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: 'sm' | 'md' }) {
  const styles: Record<Variant, string> = {
    primary: 'bg-accent text-white hover:brightness-110 border-transparent',
    secondary: 'bg-panel-2 text-text hover:bg-[#232936] border-line',
    ghost: 'bg-transparent text-muted hover:text-text hover:bg-panel-2 border-transparent',
    danger: 'bg-transparent text-bad hover:bg-[#2a1717] border-[#4a2323]'
  }
  return (
    <button
      {...props}
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition disabled:opacity-40 disabled:pointer-events-none cursor-default select-none',
        size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-9 px-3.5 text-sm',
        styles[variant],
        className
      )}
    />
  )
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx('rounded-xl border border-line bg-panel', className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="text-sm font-semibold">{title}</div>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

type Tone = 'ok' | 'warn' | 'bad' | 'muted' | 'accent'

export function Badge({ tone = 'muted', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  const tones: Record<Tone, string> = {
    ok: 'bg-[#10291f] text-ok border-[#1d4a37]',
    warn: 'bg-[#2c2410] text-warn border-[#4d3d15]',
    bad: 'bg-[#2c1515] text-bad border-[#4d2323]',
    muted: 'bg-panel-2 text-muted border-line',
    accent: 'bg-accent-soft text-accent border-[#5a3418]'
  }
  return (
    <span title={title} className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap', tones[tone])}>
      {children}
    </span>
  )
}

export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: ReactNode }) {
  return (
    <label className={cx('inline-flex items-center gap-2.5 select-none', disabled ? 'opacity-40' : 'cursor-default')}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx('relative h-5 w-9 shrink-0 rounded-full border transition', checked ? 'bg-accent border-accent' : 'bg-panel-2 border-line')}
      >
        <span className={cx('absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-all', checked ? 'left-[18px]' : 'left-0.5')} />
      </button>
      {label && <span className="text-sm">{label}</span>}
    </label>
  )
}

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <div className="text-xs font-medium text-muted">{label}</div>
      {children}
      {hint && <div className="text-xs text-muted/80 leading-relaxed">{hint}</div>}
    </div>
  )
}

const inputBase =
  'rounded-md border border-line bg-bg px-3 text-sm text-text placeholder:text-muted/60 outline-none focus:border-accent/70 disabled:opacity-50'

// Full width unless the caller sets a width: Tailwind resolves conflicting
// utilities by stylesheet order, not attribute order, so w-full would win.
function width(className?: string): string {
  return className && /(^|\s)w-/.test(className) ? '' : 'w-full'
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(inputBase, width(props.className), 'h-9', props.className)} />
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(inputBase, width(props.className), 'py-2 leading-relaxed', props.className)} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cx(inputBase, width(props.className), 'h-9 pr-8', props.className)} />
}

export function Progress({ value, total }: { value: number; total: number }) {
  const pct = total > 0 ? Math.min(100, Math.round((value / total) * 100)) : 0
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-panel-2">
      <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-line px-4 py-8 text-center text-sm text-muted">{children}</div>
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-[#4d2323] bg-[#2c1515] px-3 py-2 text-sm text-bad">{children}</div>
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        className="h-full w-[560px] max-w-[92vw] overflow-y-auto border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-line bg-panel px-5 py-4">
          <div className="min-w-0 text-base font-semibold">{title}</div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Закрыть">
            <X size={16} />
          </Button>
        </header>
        <div className="p-5">{children}</div>
      </aside>
    </div>
  )
}

export function KeyValue({ items }: { items: Array<[ReactNode, ReactNode]> }) {
  return (
    <dl className="grid grid-cols-[150px_1fr] gap-x-4 gap-y-2 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}
