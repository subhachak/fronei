import { AuthCta } from './AuthCta'

export function Hero() {
  return (
    <section className="mx-auto w-full max-w-6xl px-6 pb-20 pt-16 sm:pt-24">
      <p className="font-[family-name:var(--font-marketing-mono)] text-xs font-semibold uppercase tracking-[0.14em] text-emerald-700">
        The evidentiary research engine for regulated decisions
      </p>
      <h1 className="mt-5 max-w-3xl font-[family-name:var(--font-marketing-serif)] text-4xl font-semibold leading-[1.1] text-stone-900 sm:text-5xl lg:text-6xl">
        Every claim, traced.
        <br />
        Every conflict, named.
      </h1>
      <p className="mt-6 max-w-2xl text-lg leading-relaxed text-stone-600">
        Fronei traces every claim back to its source, flags what&rsquo;s gone stale, and surfaces
        the contradictions your team would otherwise miss — built for the diligence work
        regulators, auditors, and boards actually ask you to defend.
      </p>
      <div className="mt-9 flex flex-wrap items-center gap-4">
        <AuthCta />
        <a
          href="#demo"
          className="inline-flex items-center justify-center whitespace-nowrap rounded-lg border border-stone-300 px-5 py-2.5 text-sm font-semibold text-stone-700 transition-colors hover:border-stone-400 hover:bg-stone-100"
        >
          See it in action
        </a>
      </div>
    </section>
  )
}
