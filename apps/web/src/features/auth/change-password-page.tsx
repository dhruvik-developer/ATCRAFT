import { LogOut } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/primitives'

import { useAuth } from './auth-context'
import { ChangePasswordCard } from './profile-page'

/**
 * The deliberately small shell used while an administrator-issued temporary
 * password is still active. The API permits only identity/password/logout
 * operations in this state, so rendering the normal application chrome would
 * create a wall of expected 403 responses from its background queries.
 */
export default function ChangePasswordPage() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    if (user && !user.must_change_password) navigate('/', { replace: true })
  }, [navigate, user])

  return (
    <main className="min-h-dvh bg-muted/20 p-4 sm:p-6">
      <div className="mx-auto max-w-2xl space-y-5 pt-[max(2rem,8vh)]">
        <div>
          <p className="text-sm font-semibold tracking-wide text-primary">ATCRAFT ERP</p>
          <h1 className="mt-1 text-2xl font-semibold">Choose a new password</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Finish securing your account before opening the rest of the application.
          </p>
        </div>

        <Alert variant="warning">
          <AlertTitle>Temporary password in use</AlertTitle>
          <AlertDescription>
            This account is still using a password set by an administrator. Replace it now; all
            other signed-in devices will be closed.
          </AlertDescription>
        </Alert>

        <ChangePasswordCard autoFocus />

        <Button variant="ghost" onClick={() => void logout()}>
          <LogOut aria-hidden />
          Sign out instead
        </Button>
      </div>
    </main>
  )
}
