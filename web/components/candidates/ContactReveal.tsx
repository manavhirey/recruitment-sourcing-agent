"use client"

import { useRouter } from "next/navigation"
import { useEffect, useRef, useState } from "react"

import {
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import type { MaskedContact } from "@/lib/schemas"

const revealLifetimeMs = 60_000

export function ContactReveal({
  candidateId,
  contacts,
  runCandidateId,
  enrichmentEligible = false,
  estimatedEnrichmentCredits,
}: {
  candidateId: string
  contacts: readonly MaskedContact[]
  runCandidateId?: string | null
  enrichmentEligible?: boolean
  estimatedEnrichmentCredits?: number | null
}) {
  return <ContactRevealView key={candidateId} candidateId={candidateId} contacts={contacts} runCandidateId={runCandidateId} enrichmentEligible={enrichmentEligible} estimatedEnrichmentCredits={estimatedEnrichmentCredits} />
}

function ContactRevealView({
  candidateId,
  contacts,
  runCandidateId,
  enrichmentEligible,
  estimatedEnrichmentCredits,
}: {
  candidateId: string
  contacts: readonly MaskedContact[]
  runCandidateId?: string | null
  enrichmentEligible: boolean
  estimatedEnrichmentCredits?: number | null
}) {
  const router = useRouter()
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [enrichmentStatus, setEnrichmentStatus] = useState<string | null>(null)
  const keys = useRef(new Map<string, string>())
  const timeouts = useRef(new Set<ReturnType<typeof setTimeout>>())

  useEffect(() => {
    const activeTimeouts = timeouts.current
    return () => {
      for (const timeout of activeTimeouts) clearTimeout(timeout)
      activeTimeouts.clear()
    }
  }, [])

  async function reveal(contact: MaskedContact) {
    setBusy(contact.id)
    setError(null)
    const fingerprint = `reveal:${candidateId}:${contact.id}`
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID()
    keys.current.set(fingerprint, key)
    try {
      const response = await fetch(`/api/bff/contact-points/${contact.id}/reveal`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: "{}",
        cache: "no-store",
      })
      const result = await responseJson<{ id: string; value: string }>(response)
      keys.current.delete(fingerprint)
      setRevealed((current) => ({ ...current, [contact.id]: result.value }))
      const timeout = setTimeout(() => {
        setRevealed((current) => {
          const next = { ...current }
          delete next[contact.id]
          return next
        })
      }, revealLifetimeMs)
      timeouts.current.add(timeout)
    } catch (caught) {
      setRevealed({})
      if (reauthenticateExpiredSession(caught, router)) return
      setError("Contact could not be revealed. Retry uses the same safe request.")
    } finally {
      setBusy(null)
    }
  }

  async function enrich() {
    if (!runCandidateId) return
    setBusy("enrich")
    setError(null)
    const fingerprint = `enrich:${candidateId}:${runCandidateId}`
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID()
    keys.current.set(fingerprint, key)
    try {
      await responseJson(
        await fetch(`/api/bff/run-candidates/${runCandidateId}/enrich`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": key },
          body: "{}",
        }),
      )
      keys.current.delete(fingerprint)
      setEnrichmentStatus("Enrichment queued. Status will update with the sourcing run.")
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return
      setError("Enrichment could not be queued. Retry uses the same safe request.")
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="detail-section" aria-labelledby="contact-heading">
      <h3 id="contact-heading">Contact availability</h3>
      {contacts.length ? (
        <ul className="contact-list">
          {contacts.map((contact) => (
            <li key={contact.id}>
              <span>{revealed[contact.id] ?? contact.masked_value}</span>
              <small>{contact.verification_state}</small>
              <button
                type="button"
                className="button button-quiet"
                disabled={busy === contact.id}
                onClick={() => void reveal(contact)}
              >
                {revealed[contact.id]
                  ? "Revealed for this view"
                  : `Reveal ${contact.classification} ${contact.kind}`}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="enrichment-callout">
          <p>Contact is unavailable or was not included in automatic top-50 enrichment.</p>
          {runCandidateId && enrichmentEligible ? (
            <>
              {estimatedEnrichmentCredits ? <p><strong>Estimated cost: up to {estimatedEnrichmentCredits} provider credits.</strong></p> : null}
              <button type="button" className="button button-secondary" disabled={busy === "enrich"} onClick={() => void enrich()}>
                Enrich contact
              </button>
            </>
          ) : null}
        </div>
      )}
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      {enrichmentStatus ? <p role="status">{enrichmentStatus}</p> : null}
    </section>
  )
}
