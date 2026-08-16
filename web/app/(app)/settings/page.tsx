import { AgencyAlerts } from "@/components/layout/AgencyAlerts"
import { MembershipManager } from "@/components/layout/MembershipManager"
import { apiFetch } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import { isManager, type Client, type Member, type Notification } from "@/lib/schemas"

export const metadata = { title: "Settings" }

export default async function SettingsPage() {
  const context = await requirePageContext()
  const [alerts, clients, members] = await Promise.all([
    apiFetch<Notification[]>("/api/v1/notifications", context.tenantId),
    isManager(context.me.role)
      ? apiFetch<Client[]>("/api/v1/clients", context.tenantId)
      : Promise.resolve([]),
    isManager(context.me.role)
      ? apiFetch<Member[]>("/api/v1/members", context.tenantId)
      : Promise.resolve([]),
  ])
  return (
    <div className="page-stack">
      <header className="page-header"><div><p className="eyebrow">Agency controls</p><h1>Settings</h1></div></header>
      <section className="settings-card"><h2>Workspace access</h2><p>Your current role is <strong>{context.me.role}</strong>.</p><p>Provider credentials and operator controls are never exposed in agency settings.</p></section>
      <AgencyAlerts alerts={alerts} />
      <MembershipManager role={context.me.role} members={members} clients={clients} />
    </div>
  )
}
