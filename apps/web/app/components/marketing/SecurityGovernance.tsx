const CONTROLS = [
  {
    title: 'OWASP LLM Top 10',
    body: 'Prompt-injection defenses and output handling controls mapped against the OWASP Top 10 for LLM applications.',
  },
  {
    title: 'NIST AI RMF',
    body: 'Governance practices aligned to the NIST AI Risk Management Framework’s govern-map-measure-manage cycle.',
  },
  {
    title: 'SOC 2-aligned',
    body: 'Security, availability, and confidentiality controls built to a SOC 2-aligned operating standard.',
  },
]

export function SecurityGovernance() {
  return (
    <section id="security" className="mx-auto w-full max-w-6xl scroll-mt-20 px-6 py-20">
      <h2 className="font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-900 sm:text-4xl">
        Security & governance
      </h2>
      <p className="mt-3 max-w-2xl text-stone-600">
        Built for teams whose research outputs feed into decisions that get audited.
      </p>
      <div className="mt-10 grid gap-5 sm:grid-cols-3">
        {CONTROLS.map(control => (
          <div key={control.title} className="rounded-2xl border border-stone-200 bg-white p-6">
            <h3 className="text-base font-semibold text-stone-900">{control.title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-stone-600">{control.body}</p>
          </div>
        ))}
      </div>
    </section>
  )
}
