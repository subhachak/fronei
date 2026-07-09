const CARDS = [
  {
    status: 'stale',
    accent: 'border-amber-200 bg-amber-50 text-amber-800',
    title: 'Staleness',
    body: 'Sources have a shelf life. Fronei tracks how old each piece of evidence is relative to how fast that domain changes, and flags anything you’d be wrong to treat as current.',
  },
  {
    status: 'conflict',
    accent: 'border-red-200 bg-red-50 text-red-800',
    title: 'Silent conflict-blending',
    body: 'Most tools average away disagreement between sources. Fronei surfaces the contradiction explicitly instead of quietly picking a side for you.',
  },
  {
    status: 'unverified',
    accent: 'border-stone-300 bg-stone-100 text-stone-700',
    title: 'Unverified claims',
    body: 'Every assertion is checked against its cited source before it reaches you. Claims that can’t be verified are labeled as such, not presented with false confidence.',
  },
]

export function WhatItCatches() {
  return (
    <section id="what-it-catches" className="mx-auto w-full max-w-6xl scroll-mt-20 px-6 py-20">
      <h2 className="font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-900 sm:text-4xl">
        What it catches
      </h2>
      <p className="mt-3 max-w-2xl text-stone-600">
        The failure modes that make research outputs untrustworthy in a regulated setting —
        and that most tools don&rsquo;t even try to surface.
      </p>
      <div className="mt-10 grid gap-5 sm:grid-cols-3">
        {CARDS.map(card => (
          <div key={card.title} className="rounded-2xl border border-stone-200 bg-white p-6">
            <span className={`inline-block rounded-full border px-2.5 py-1 font-[family-name:var(--font-marketing-mono)] text-[11px] font-bold uppercase tracking-wide ${card.accent}`}>
              {card.status}
            </span>
            <h3 className="mt-4 text-lg font-semibold text-stone-900">{card.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-stone-600">{card.body}</p>
          </div>
        ))}
      </div>
    </section>
  )
}
