"use client"

import { useRouter } from "next/navigation"
import { useRef, useState } from "react"

import {
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import type { Notification } from "@/lib/schemas"

export function formatUtcTimestamp(value: string): string {
  const iso = new Date(value).toISOString()
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`
}

export function AgencyAlerts({ alerts: initialAlerts }: { alerts: readonly Notification[] }) {
  const router = useRouter()
  const [alerts, setAlerts] = useState([...initialAlerts])
  const [error, setError] = useState("")
  const [busy, setBusy] = useState<string | null>(null)
  const keys = useRef(new Map<string, string>())
  const unread = alerts.filter((alert) => !alert.acknowledged_at)

  async function acknowledge(alert: Notification) {
    setBusy(alert.id)
    setError("")
    const key = keys.current.get(alert.id) ?? crypto.randomUUID()
    keys.current.set(alert.id, key)
    try {
      const updated = await responseJson<Notification>(
        await fetch(`/api/bff/notifications/${alert.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", "Idempotency-Key": key },
          body: "{}",
        }),
      )
      keys.current.delete(alert.id)
      setAlerts((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return
      setError("The alert was not acknowledged. Retry uses the same safe request.")
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="settings-card" aria-labelledby="agency-alerts-heading">
      <div className="settings-heading"><div><p className="eyebrow">Tenant alerts</p><h2 id="agency-alerts-heading">Unread alerts</h2></div><span className="count-badge">{unread.length}</span></div>
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      {unread.length ? (
        <ul className="alert-list">{unread.map((alert) => (
          <li key={alert.id}>
            <div><strong>{alert.title}</strong><p>{alert.message}</p><time dateTime={alert.created_at}>{formatUtcTimestamp(alert.created_at)}</time></div>
            <div className="alert-actions">
              {alert.code === "usage_budget_exhausted" && alert.run_id ? <a className="button button-quiet" href={`/jobs/activity?run=${alert.run_id}`}>Open Activity</a> : null}
              <button type="button" className="button button-secondary" disabled={busy === alert.id} onClick={() => void acknowledge(alert)}>Acknowledge</button>
            </div>
          </li>
        ))}</ul>
      ) : <p>No unread tenant alerts.</p>}
    </section>
  )
}
