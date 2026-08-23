"use client"

import { useEffect, useState } from "react"

export function InvitationBootstrap() {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    let invitationToken = window.location.hash.startsWith("#")
      ? decodeURIComponent(window.location.hash.slice(1))
      : ""
    window.history.replaceState(null, "", "/invite")

    void (async () => {
      try {
        const body = JSON.stringify({ token: invitationToken })
        invitationToken = ""
        const response = await fetch("/invite/capture", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
          cache: "no-store",
          credentials: "same-origin",
          referrerPolicy: "no-referrer",
          signal: controller.signal,
        })
        if (!response.ok) throw new Error("invitation_invalid")
        window.location.replace("/invite/claim")
      } catch (error) {
        invitationToken = ""
        if (error instanceof DOMException && error.name === "AbortError") return
        setFailed(true)
      }
    })()

    return () => {
      invitationToken = ""
      controller.abort()
    }
  }, [])

  return (
    <main className="auth-page" id="main-content">
      <section className="auth-card" aria-labelledby="invitation-heading">
        <p className="eyebrow">Agency invitation</p>
        <h1 id="invitation-heading">Accept invitation</h1>
        {failed
          ? <p role="alert">This invitation is invalid or expired.</p>
          : <p role="status">Securing your invitation…</p>}
      </section>
    </main>
  )
}
