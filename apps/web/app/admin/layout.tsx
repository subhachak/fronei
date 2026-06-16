import '../globals.css'
import '../v2/v2-tokens.css'
import { currentUser } from '@clerk/nextjs/server'
import { redirect } from 'next/navigation'

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const user = await currentUser()
  if (!user) redirect('/sign-in')

  const role = (user.publicMetadata as { role?: string })?.role
  if (role !== 'admin') redirect('/')

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="flex items-center gap-3 border-b border-border px-6 py-3">
        <img src="/fronei-icon.svg" alt="Fronei" className="h-6 w-6" />
        <span className="text-sm font-semibold">Fronei Admin</span>
      </header>
      <div className="p-6">{children}</div>
    </div>
  )
}
