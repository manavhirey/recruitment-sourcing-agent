"use client"

import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"
import { useState, type ReactNode } from "react"

import type { Role } from "@/lib/schemas"
import type { TenantOption } from "@/lib/auth-config"
import {
  reauthenticateExpiredSession,
  requireResponse,
} from "@/lib/client-response"

type ShellJob = { id: string; title: string; status: string }

type AppShellProps = {
  agency: TenantOption
  user: { name?: string | null; email?: string | null }
  role: Role
  tenantOptions: readonly TenantOption[]
  activeJobs: readonly ShellJob[]
  children: ReactNode
}

const navigation = [
  { href: "/jobs", label: "Jobs" },
  { href: "/candidates", label: "Candidates" },
  { href: "/clients", label: "Clients" },
  { href: "/settings", label: "Settings" },
]

export function AppShell({
  agency,
  user,
  role,
  tenantOptions,
  activeJobs,
  children,
}: AppShellProps) {
  const pathname = usePathname()
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="topbar">
        <Link className="brand" href="/jobs" aria-label="Sourcing Desk home">
          <span className="brand-mark" aria-hidden="true">N</span>
          <span>Sourcing Desk</span>
        </Link>
        <nav aria-label="Primary" className="primary-nav">
          {navigation.map((item) => (
            <Link
              href={item.href}
              key={item.href}
              aria-current={pathname?.startsWith(item.href) ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="topbar-actions">
          <AgencySwitcher agency={agency} options={tenantOptions} />
          <details className="user-menu">
            <summary aria-label="Open user menu">
              <span aria-hidden="true">{(user.name ?? user.email ?? "U").slice(0, 1).toUpperCase()}</span>
            </summary>
            <div className="user-popover">
              <strong>{user.name ?? "Agency user"}</strong>
              {user.email ? <span>{user.email}</span> : null}
              <span className="role-label">{role}</span>
              <Link href="/api/auth/signout">Sign out</Link>
            </div>
          </details>
        </div>
      </header>
      <div className="shell-body">
        <aside className="job-rail" aria-label="Active jobs">
          <div className="rail-heading">
            <span>Active jobs</span>
            <Link href="/jobs/new" aria-label="Create a job">+</Link>
          </div>
          {activeJobs.length === 0 ? (
            <p className="rail-empty">No active jobs yet.</p>
          ) : (
            <ul>
              {activeJobs.map((job) => (
                <li key={job.id}>
                  <Link href={`/jobs/${job.id}`}>
                    <span>{job.title}</span>
                    <small>{job.status.replaceAll("_", " ")}</small>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </aside>
        <main id="main-content" tabIndex={-1}>{children}</main>
      </div>
    </div>
  )
}

function AgencySwitcher({
  agency,
  options,
}: {
  agency: TenantOption
  options: readonly TenantOption[]
}) {
  const router = useRouter()
  const [switching, setSwitching] = useState(false)
  const [switchError, setSwitchError] = useState(false)
  if (options.length <= 1) {
    return <span className="agency-name">{agency.name}</span>
  }
  return (
    <div className="agency-switcher">
      <label className="sr-only" htmlFor="agency-switcher">Agency</label>
      <select
        id="agency-switcher"
        value={agency.id}
        disabled={switching}
        onChange={async (event) => {
          setSwitching(true)
          setSwitchError(false)
          try {
            const response = await fetch("/api/tenant", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tenantId: event.target.value }),
            })
            await requireResponse(response)
            router.push("/jobs")
            router.refresh()
            return
          } catch (caught) {
            if (reauthenticateExpiredSession(caught, router)) return
            // The selected agency and raw failure details remain private.
          }
          setSwitchError(true)
          setSwitching(false)
        }}
      >
        {options.map((option) => (
          <option key={option.id} value={option.id}>{option.name}</option>
        ))}
      </select>
      {switchError ? (
        <p className="sr-only" role="alert">Agency switching is unavailable. Try again.</p>
      ) : null}
    </div>
  )
}
