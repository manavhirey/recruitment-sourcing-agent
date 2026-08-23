"use client"

import type { ReactNode, RefObject } from "react"
import { useEffect, useRef } from "react"

const focusableSelector = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",")

export function ModalDialog({
  labelledBy,
  initialFocus,
  onClose,
  children,
}: {
  labelledBy: string
  initialFocus?: RefObject<HTMLElement | null>
  onClose: () => void
  children: ReactNode
}) {
  const dialog = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const panel = dialog.current
    ;(initialFocus?.current ?? panel)?.focus()
    return () => {
      if (opener?.isConnected) opener.focus()
    }
  }, [initialFocus])

  return (
    <div className="dialog-backdrop">
      <div
        ref={dialog}
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault()
            onClose()
            return
          }
          if (event.key !== "Tab") return
          const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])]
          if (!focusable.length) {
            event.preventDefault()
            dialog.current?.focus()
            return
          }
          const first = focusable[0]
          const last = focusable.at(-1) ?? first
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault()
            last.focus()
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault()
            first.focus()
          }
        }}
      >
        {children}
      </div>
    </div>
  )
}
