import { ClientManager } from "@/components/clients/ClientManager"
import { apiFetch } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import { isManager, type Client, type Member } from "@/lib/schemas"

export const metadata = { title: "Clients" }

export default async function ClientsPage() {
  const context = await requirePageContext()
  const clients = await apiFetch<Client[]>("/api/v1/clients", context.tenantId)
  const members = isManager(context.me.role)
    ? await apiFetch<Member[]>("/api/v1/members", context.tenantId)
    : []
  const grantMembers = members.map((member) => ({
    membership_id: member.membership_id,
    display_name: member.display_name,
    role: member.role,
    allowed_client_ids: member.allowed_client_ids,
    active: member.active,
  }))
  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Agency accounts</p>
          <h1>Clients</h1>
          <p>{context.me.role === "recruiter" ? "Clients currently granted to you." : "Control industry context and recruiter access."}</p>
        </div>
      </header>
      <ClientManager clients={clients} members={grantMembers} role={context.me.role} />
    </div>
  )
}
