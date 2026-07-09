import Image from 'next/image'
import Link from 'next/link'
import { brandAsset } from '../../lib/brand'
import { AuthCta } from './AuthCta'

export function MarketingNav() {
  return (
    <header className="border-b border-stone-200 bg-stone-50/90 backdrop-blur">
      <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-6">
        <Link href="/" className="flex items-center gap-2.5">
          <Image src={brandAsset('/fronei-icon.svg')} alt="Fronei" width={28} height={28} priority />
          <span className="font-[family-name:var(--font-marketing-serif)] text-lg font-semibold text-stone-900">
            Fronei
          </span>
        </Link>
        <nav className="hidden items-center gap-8 text-sm font-medium text-stone-600 sm:flex">
          <a href="#what-it-catches" className="hover:text-stone-900">What it catches</a>
          <a href="#domains" className="hover:text-stone-900">Platform</a>
          <a href="#how-it-works" className="hover:text-stone-900">How it works</a>
          <a href="#security" className="hover:text-stone-900">Security</a>
        </nav>
        <AuthCta variant="compact" />
      </div>
    </header>
  )
}
