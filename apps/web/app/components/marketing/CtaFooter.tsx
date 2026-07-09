import Link from 'next/link'
import { AuthCta } from './AuthCta'

export function CtaFooter() {
  return (
    <>
      <section className="bg-stone-900">
        <div className="mx-auto w-full max-w-6xl px-6 py-16 text-center">
          <h2 className="font-[family-name:var(--font-marketing-serif)] text-3xl font-semibold text-stone-50 sm:text-4xl">
            Research your team can defend.
          </h2>
          <p className="mx-auto mt-3 max-w-xl text-stone-300">
            Every claim traced. Every conflict named. Every source dated.
          </p>
          <div className="mt-8 flex justify-center">
            <AuthCta />
          </div>
        </div>
      </section>
      <footer className="border-t border-stone-200 bg-stone-50">
        <div className="mx-auto flex w-full max-w-6xl flex-col items-center justify-between gap-4 px-6 py-8 text-sm text-stone-500 sm:flex-row">
          <span>&copy; {new Date().getFullYear()} Fronei.</span>
          <div className="flex items-center gap-6">
            <Link href="/sign-in" className="hover:text-stone-900">Sign in</Link>
            <a href="#security" className="hover:text-stone-900">Security</a>
          </div>
        </div>
      </footer>
    </>
  )
}
