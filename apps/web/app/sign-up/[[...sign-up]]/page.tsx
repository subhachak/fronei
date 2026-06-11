'use client'
import { SignUp } from '@clerk/nextjs'

export default function SignUpPage() {
  return (
    <div style={{
      display: 'flex',
      height: '100vh',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'var(--bg-base)',
    }}>
      <SignUp
        appearance={{
          variables: { colorPrimary: '#7c3aed', borderRadius: '12px' },
          elements: {
            card: { background: 'var(--bg-s1)', border: '1px solid var(--bd2)', boxShadow: 'none' },
            headerTitle: { color: 'var(--t1)' },
            socialButtonsBlockButton: { border: '1px solid var(--bd2)' },
          },
        }}
      />
    </div>
  )
}
