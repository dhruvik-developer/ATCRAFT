import { LoadError, PanelLoader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export interface LazyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function LazyDialogFallback({
  open,
  onOpenChange,
  onRetry,
}: LazyDialogProps & { onRetry?: () => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-busy={!onRetry}>
        <DialogHeader>
          <DialogTitle>{onRetry ? 'Could not open this form' : 'Loading form'}</DialogTitle>
          <DialogDescription>
            {onRetry ? 'Try opening the form again.' : 'Preparing this form.'}
          </DialogDescription>
        </DialogHeader>
        {onRetry ? <LoadError error={undefined} onRetry={onRetry} /> : <PanelLoader />}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
