const REPORT_ROWS = [
  { claim: 'Data encrypted at rest and in transit (AES-256)', status: 'verified' as const },
  { claim: 'No reportable security incidents in trailing 24 months', status: 'conflict' as const },
  { claim: 'Annual penetration test current as of last quarter', status: 'stale' as const },
]

const STATUS_DOT: Record<string, string> = {
  verified: 'bg-emerald-600',
  conflict: 'bg-red-600',
  stale: 'bg-amber-600',
}

const STATUS_LABEL: Record<string, string> = {
  verified: 'Verified',
  conflict: 'Conflict',
  stale: 'Stale',
}

export function FlagshipWorkflow() {
  return (
    <section className="mx-auto w-full max-w-6xl px-6 py-20">
      <div className="grid gap-12 lg:grid-cols-2 lg:items-center">
        <div>
          <span className="inline-block rounded-full bg-stone-900 px-2.5 py-1 font-[family-name:var(--font-marketing-mono)] text-[10px] font-bold uppercase tracking-wide text-emerald-300">
            Flagship workflow
          </span>
          <h2 className="mt-4 font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-900 sm:text-4xl">
            Vendor & tech risk due diligence
          </h2>
          <p className="mt-4 leading-relaxed text-stone-600">
            Point Fronei at a vendor and it builds a due-diligence report the way a skeptical
            reviewer would: every control claim checked against the vendor&rsquo;s own filings,
            every contradiction with public disclosures called out by name, and every source
            timestamped so your team knows exactly how current the evidence is.
          </p>
          <p className="mt-4 leading-relaxed text-stone-600">
            The output is exportable and audit-ready — a report you can hand to a reviewer
            without having to first re-verify it yourself.
          </p>
        </div>

        <div className="overflow-hidden rounded-2xl border border-stone-300 bg-white shadow-lg">
          <div className="flex items-center gap-1.5 border-b border-stone-200 bg-stone-100 px-4 py-3">
            <span className="h-2.5 w-2.5 rounded-full bg-stone-300" />
            <span className="h-2.5 w-2.5 rounded-full bg-stone-300" />
            <span className="h-2.5 w-2.5 rounded-full bg-stone-300" />
            <span className="ml-3 font-[family-name:var(--font-marketing-mono)] text-xs text-stone-500">
              Vendor_Risk_Report.pdf
            </span>
          </div>
          <div className="p-5">
            <p className="font-[family-name:var(--font-marketing-serif)] text-lg font-semibold text-stone-900">
              Security & Compliance Findings
            </p>
            <div className="mt-4 grid gap-3">
              {REPORT_ROWS.map(row => (
                <div key={row.claim} className="flex items-start justify-between gap-4 rounded-lg border border-stone-100 bg-stone-50 p-3">
                  <span className="text-sm text-stone-700">{row.claim}</span>
                  <span className="flex shrink-0 items-center gap-1.5 font-[family-name:var(--font-marketing-mono)] text-[11px] font-bold uppercase tracking-wide text-stone-500">
                    <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[row.status]}`} />
                    {STATUS_LABEL[row.status]}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
