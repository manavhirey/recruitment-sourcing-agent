import type { Metadata } from "next"
import type { ReactNode } from "react"

import "./globals.css"

export const metadata: Metadata = {
  title: { default: "Sourcing Desk", template: "%s · Sourcing Desk" },
  description: "Evidence-led recruitment sourcing workspace",
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
