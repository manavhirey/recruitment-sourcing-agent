"use client"

import { useRouter } from "next/navigation"
import { useEffect, useRef, useState } from "react"

import {
  ClientResponseError,
  reauthenticateExpiredSession,
  requireResponse,
  responseJson,
} from "@/lib/client-response"
import { isManager, type Client, type Invitation, type Member, type Role } from "@/lib/schemas"
import { formatUtcTimestamp } from "@/components/layout/AgencyAlerts"

type EphemeralInvitation = Invitation & { link: string }

export function MembershipManager({
  role,
  members: initialMembers,
  clients,
}: {
  role: Role
  members: readonly Member[]
  clients: readonly Client[]
}) {
  const router = useRouter()
  const [members, setMembers] = useState([...initialMembers])
  const [email, setEmail] = useState("")
  const [inviteRole, setInviteRole] = useState<"admin" | "recruiter">("recruiter")
  const [invitation, setInvitation] = useState<EphemeralInvitation | null>(null)
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const [status, setStatus] = useState("")
  const keys = useRef(new Map<string, string>())
  const expiryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (expiryTimer.current) clearTimeout(expiryTimer.current)
  }, [])

  if (!isManager(role)) {
    return <section className="settings-card"><h2>Membership</h2><p>Only agency owners and admins can manage membership.</p></section>
  }

  async function createInvitation(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = email.trim().toLowerCase()
    if (!normalized || busy) return
    const fingerprint = `invite:${normalized}:${inviteRole}`
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID()
    keys.current.set(fingerprint, key)
    setBusy(fingerprint)
    setError("")
    if (expiryTimer.current) {
      clearTimeout(expiryTimer.current)
      expiryTimer.current = null
    }
    setInvitation(null)
    try {
      const result = await responseJson<Invitation>(
        await fetch("/api/bff/membership-invitations", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": key },
          body: JSON.stringify({ email: normalized, role: inviteRole }),
          cache: "no-store",
        }),
      )
      keys.current.delete(fingerprint)
      const link = `${window.location.origin}/invite#${encodeURIComponent(result.token)}`
      setInvitation({ ...result, link })
      setEmail("")
      const expiresIn = Math.max(0, Math.min(
        new Date(result.expires_at).getTime() - Date.now(),
        2_147_483_647,
      ))
      expiryTimer.current = setTimeout(() => {
        expiryTimer.current = null
        setInvitation(null)
      }, expiresIn)
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return
      setError("The invitation could not be created. Retry uses the same safe request.")
    } finally {
      setBusy("")
    }
  }

  async function mutate(
    fingerprint: string,
    path: string,
    method: "POST" | "PATCH" | "DELETE",
    body: Record<string, unknown>,
    success: string,
  ): Promise<boolean> {
    if (busy) return false
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID()
    keys.current.set(fingerprint, key)
    setBusy(fingerprint)
    setError("")
    setStatus("")
    try {
      await requireResponse(await fetch(path, {
        method,
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: "{}" === JSON.stringify(body) ? "{}" : JSON.stringify(body),
      }))
      keys.current.delete(fingerprint)
      setStatus(success)
      return true
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return false
      setError(
        caught instanceof ClientResponseError && caught.code === "last_owner_required"
          ? "The last active owner cannot be demoted or deactivated."
          : "The membership change was not saved. Retry uses the same safe request.",
      )
      return false
    } finally {
      setBusy("")
    }
  }

  return (
    <section className="settings-card membership-manager" aria-labelledby="membership-heading">
      <div className="settings-heading"><div><p className="eyebrow">Agency access</p><h2 id="membership-heading">Membership</h2></div></div>
      <form className="invite-form" onSubmit={(event) => void createInvitation(event)}>
        <div className="field"><label htmlFor="invitation-email">Invitation email</label><input id="invitation-email" type="email" required maxLength={320} value={email} onChange={(event) => setEmail(event.target.value)} /></div>
        <div className="field"><label htmlFor="invitation-role">Role</label><select id="invitation-role" value={inviteRole} onChange={(event) => setInviteRole(event.target.value as "admin" | "recruiter")}><option value="recruiter">Recruiter</option><option value="admin">Admin</option></select></div>
        <button type="submit" className="button button-primary" disabled={Boolean(busy)}>Create invitation</button>
      </form>
      {invitation ? (
        <div className="invitation-result" role="status">
          <p><strong>One-time invitation link</strong></p>
          <code>{invitation.link}</code>
          <p>Expires {formatUtcTimestamp(invitation.expires_at)}. It cannot be recovered after this view clears.</p>
          <button type="button" className="button button-secondary" onClick={async () => {
            try {
              await navigator.clipboard.writeText(invitation.link)
              setStatus("Invitation copied. The on-screen copy was cleared.")
            } finally {
              if (expiryTimer.current) clearTimeout(expiryTimer.current)
              expiryTimer.current = null
              setInvitation(null)
            }
          }}>Copy once</button>
          <button type="button" className="button button-quiet" onClick={() => {
            if (expiryTimer.current) clearTimeout(expiryTimer.current)
            expiryTimer.current = null
            setInvitation(null)
          }}>Clear now</button>
        </div>
      ) : null}
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <p className="sr-only" role="status" aria-live="polite">{status}</p>
      <ul className="member-list">{members.map((member) => (
        <li key={member.membership_id}>
          <div className="member-summary"><strong>{member.display_name}</strong><span>{member.email}</span><small>{member.active ? member.role : "deactivated"}</small></div>
          {member.active ? (
            <div className="member-controls">
              {member.role !== "owner" ? (
                <label>Role<select aria-label={`Role for ${member.display_name}`} value={member.role} onChange={async (event) => {
                  const nextRole = event.target.value as "admin" | "recruiter"
                  const ok = await mutate(`role:${member.membership_id}:${nextRole}`, `/api/bff/members/${member.membership_id}/role`, "PATCH", { role: nextRole }, "Role updated.")
                  if (ok) setMembers((current) => current.map((item) => item.membership_id === member.membership_id ? { ...item, role: nextRole } : item))
                }}><option value="recruiter">Recruiter</option><option value="admin">Admin</option></select></label>
              ) : <span>Owner</span>}
              {member.role === "recruiter" ? (
                <fieldset><legend>Allowed clients</legend>{clients.map((client) => {
                  const checked = member.allowed_client_ids?.includes(client.id) ?? false
                  return <label key={client.id}><input type="checkbox" checked={checked} onChange={async (event) => {
                    const grant = event.target.checked
                    const fingerprint = `client:${member.membership_id}:${client.id}:${grant ? "grant" : "revoke"}`
                    const path = grant ? `/api/bff/clients/${client.id}/grants` : `/api/bff/clients/${client.id}/grants/${member.membership_id}`
                    const ok = await mutate(fingerprint, path, grant ? "POST" : "DELETE", grant ? { membership_id: member.membership_id } : {}, grant ? "Client access granted." : "Client access revoked.")
                    if (ok) setMembers((current) => current.map((item) => item.membership_id === member.membership_id ? { ...item, allowed_client_ids: grant ? [...(item.allowed_client_ids ?? []), client.id] : (item.allowed_client_ids ?? []).filter((id) => id !== client.id) } : item))
                  }} />{client.name}</label>
                })}</fieldset>
              ) : null}
              <button type="button" className="button button-danger-quiet" onClick={async () => {
                const ok = await mutate(`deactivate:${member.membership_id}`, `/api/bff/members/${member.membership_id}`, "DELETE", {}, "Membership deactivated.")
                if (ok) setMembers((current) => current.map((item) => item.membership_id === member.membership_id ? { ...item, active: false } : item))
              }}>Deactivate {member.display_name}</button>
            </div>
          ) : null}
        </li>
      ))}</ul>
    </section>
  )
}
