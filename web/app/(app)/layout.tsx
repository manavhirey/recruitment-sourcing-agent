import type { ReactNode } from "react"

import { AppShell } from "@/components/layout/AppShell"
import { QueryProvider } from "@/components/layout/QueryProvider"
import { requirePageContext } from "@/lib/page-context"

export default async function AuthenticatedLayout({ children }: { children: ReactNode }) {
  const context = await requirePageContext()
  return (
    <QueryProvider>
      <AppShell
        agency={context.agency}
        user={context.session.user ?? {}}
        role={context.me.role}
        tenantOptions={context.tenantOptions}
        activeJobs={context.jobs.items.map((job) => ({
          id: job.id,
          title: job.title,
          status: job.status,
        }))}
      >
        {children}
      </AppShell>
    </QueryProvider>
  )
}
