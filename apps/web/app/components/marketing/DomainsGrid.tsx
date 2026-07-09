const DOMAINS = [
  {
    name: 'Vendor & tech risk',
    flagship: true,
    body: 'Third-party due diligence with verified evidence, conflict surfacing, and staleness flags — Fronei’s flagship workflow.',
  },
  { name: 'Legal', body: 'Case law and contract research with the same source-verification discipline.' },
  { name: 'Financial / M&A', body: 'Diligence research for deal teams who need traceable, defensible sourcing.' },
  { name: 'Compliance', body: 'Regulatory research that flags what’s outdated before it reaches a filing.' },
  { name: 'Market intelligence', body: 'Competitive and market research without silently blended, unattributed claims.' },
]

export function DomainsGrid() {
  return (
    <section id="domains" className="bg-stone-100/70">
      <div className="mx-auto w-full max-w-6xl scroll-mt-20 px-6 py-20">
        <h2 className="font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-900 sm:text-4xl">
          One engine, five regulated domains
        </h2>
        <p className="mt-3 max-w-2xl text-stone-600">
          The same evidentiary core — verification, conflict detection, staleness tracking —
          applied wherever a decision needs to hold up under scrutiny.
        </p>
        <div className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {DOMAINS.map(domain => (
            <div
              key={domain.name}
              className={`rounded-2xl border p-6 ${domain.flagship ? 'border-stone-900 bg-stone-900 text-stone-50' : 'border-stone-200 bg-white'}`}
            >
              <div className="flex items-center justify-between gap-2">
                <h3 className={`text-lg font-semibold ${domain.flagship ? 'text-stone-50' : 'text-stone-900'}`}>
                  {domain.name}
                </h3>
                {domain.flagship && (
                  <span className="rounded-full bg-emerald-500/20 px-2.5 py-1 font-[family-name:var(--font-marketing-mono)] text-[10px] font-bold uppercase tracking-wide text-emerald-300">
                    Flagship
                  </span>
                )}
              </div>
              <p className={`mt-2 text-sm leading-relaxed ${domain.flagship ? 'text-stone-300' : 'text-stone-600'}`}>
                {domain.body}
              </p>
              {!domain.flagship && (
                <p className="mt-4 font-[family-name:var(--font-marketing-mono)] text-[11px] font-semibold uppercase tracking-wide text-stone-400">
                  {/* TODO: confirm domain availability copy before launch */}
                  Built on the same engine
                </p>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
