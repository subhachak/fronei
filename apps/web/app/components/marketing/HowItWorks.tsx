const STEPS = [
  { step: '01', title: 'Plan', body: 'Decomposes the request into a research plan scoped to what actually needs answering.' },
  { step: '02', title: 'Search', body: 'Gathers evidence across sources, tracking provenance for every excerpt it pulls in.' },
  { step: '03', title: 'Verify', body: 'Checks each claim against its cited source before it’s allowed into the output.' },
  { step: '04', title: 'Flag', body: 'Surfaces conflicts between sources and flags evidence that’s aged past reliable use.' },
  { step: '05', title: 'Deliver', body: 'Produces a report where every claim carries its citation, status, and confidence.' },
]

export function HowItWorks() {
  return (
    <section id="how-it-works" className="mx-auto w-full max-w-6xl scroll-mt-20 px-6 py-20">
      <h2 className="font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-900 sm:text-4xl">
        How it works
      </h2>
      <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-5">
        {STEPS.map(item => (
          <div key={item.step} className="relative rounded-2xl border border-stone-200 bg-white p-5">
            <span className="font-[family-name:var(--font-marketing-mono)] text-2xl font-bold text-stone-200">
              {item.step}
            </span>
            <h3 className="mt-2 text-base font-semibold text-stone-900">{item.title}</h3>
            <p className="mt-1.5 text-sm leading-relaxed text-stone-600">{item.body}</p>
          </div>
        ))}
      </div>
    </section>
  )
}
