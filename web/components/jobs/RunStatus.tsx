"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useRouter } from "next/navigation"
import { useEffect, useRef, useState } from "react"

import {
  ClientResponseError,
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import { ModalDialog } from "@/components/layout/ModalDialog"
import type { SourcingRun } from "@/lib/schemas"

const terminalStates = new Set(["ready", "cancelled", "failed"])
const publicErrors: Record<string, string> = {
  usage_budget_exhausted: "The configured sourcing usage budget was exhausted.",
  provider_search_failed: "The sourcing provider could not complete the search.",
  no_usable_results: "The provider returned no usable candidates.",
  scorecard_seniority_revision_required:
    "Revise this scorecard's seniority to Early-Career, Mid-Level, or Senior before sourcing again.",
}

export function runPollingInterval(state: string): number | false {
  if (terminalStates.has(state)) return false
  return state === "partially_ready" ? 10_000 : 3_000
}

export function RunStatus({
  jobId,
  initialRun,
}: {
  jobId: string
  initialRun: SourcingRun
}) {
  const router = useRouter()
  const queryClient = useQueryClient()
  const previousMatched = useRef(initialRun.matched_count)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const [confirming, setConfirming] = useState(false)
  const cancelKey = useRef<string | null>(null)
  const cancelPending = useRef(false)
  const [announcement, setAnnouncement] = useState("")
  const query = useQuery({
    queryKey: ["run", initialRun.id],
    initialData: initialRun,
    queryFn: async () => responseJson<SourcingRun>(await fetch(`/api/bff/runs/${initialRun.id}`)),
    refetchInterval: ({ state }) => runPollingInterval(
      (state.data as SourcingRun | undefined)?.state ?? initialRun.state,
    ),
  })
  const run = query.data

  useEffect(() => {
    if (run.matched_count !== previousMatched.current) {
      previousMatched.current = run.matched_count
      void queryClient.invalidateQueries({ queryKey: ["job-candidates", jobId] })
    }
  }, [jobId, queryClient, run.matched_count])

  const cancellation = useMutation({
    mutationFn: async (idempotencyKey: string) => responseJson<SourcingRun>(
      await fetch(`/api/bff/runs/${run.id}/cancel`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: "{}",
      }),
    ),
    retry: (count, error) => count < 2 && (
      !(error instanceof ClientResponseError) || error.status >= 500
    ),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["run", run.id] })
      const previous = queryClient.getQueryData<SourcingRun>(["run", run.id])
      queryClient.setQueryData<SourcingRun>(["run", run.id], (current) =>
        current ? { ...current, cancellation_requested: true } : current,
      )
      return { previous }
    },
    onError: (error, _key, context) => {
      if (context?.previous) queryClient.setQueryData(["run", run.id], context.previous)
      if (reauthenticateExpiredSession(error, router)) return
      setAnnouncement("Cancellation was not confirmed. Available candidates are unchanged.")
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["run", run.id], updated)
      cancelKey.current = null
      setAnnouncement("Sourcing cancelled. Available candidates remain reviewable.")
    },
    onSettled: () => {
      cancelPending.current = false
      void queryClient.invalidateQueries({ queryKey: ["run", run.id] })
    },
  })

  const errorMessage = run.error_code ? publicErrors[run.error_code] : undefined
  const budget = run.budget_use
  return (
    <section className="run-status" aria-labelledby="run-status-heading">
      <div className="run-status-heading">
        <div>
          <p className="eyebrow">Sourcing run</p>
          <h2 id="run-status-heading">{run.state.replaceAll("_", " ")}</h2>
        </div>
        {!terminalStates.has(run.state) && !run.cancellation_requested && !cancellation.isPending ? (
          <button
            type="button"
            className="button button-danger-quiet"
            onClick={() => {
              cancelKey.current ??= crypto.randomUUID()
              setConfirming(true)
            }}
          >
            Cancel sourcing
          </button>
        ) : null}
      </div>
      <dl className="run-metrics">
        <div><dt>Sourced</dt><dd>{run.candidate_count}</dd></div>
        <div><dt>Matched</dt><dd>{run.matched_count}</dd></div>
        <div><dt>Enriched</dt><dd>{run.enriched_count}</dd></div>
        <div><dt>Failed</dt><dd>{run.failed_count}</dd></div>
        <div><dt>Budget used</dt><dd>{budget.estimated_credits ?? 0} credits</dd></div>
      </dl>
      {errorMessage ? <p className="run-warning" role="status">{errorMessage}</p> : null}
      {query.isError ? <p className="form-error" role="alert">Run status is temporarily unavailable.</p> : null}
      <p className="sr-only" aria-live="polite">{announcement}</p>
      {confirming ? (
        <ModalDialog
          labelledBy="cancel-heading"
          initialFocus={confirmButton}
          onClose={() => {
            cancelKey.current = null
            setConfirming(false)
          }}
        >
            <h3 id="cancel-heading">Cancel sourcing run?</h3>
            <p>New work will stop. Candidates already available will stay in this workspace.</p>
            <div className="dialog-actions">
              <button type="button" className="button button-secondary" onClick={() => {
                cancelKey.current = null
                setConfirming(false)
              }}>Keep running</button>
              <button
                ref={confirmButton}
                type="button"
                className="button button-primary"
                disabled={cancellation.isPending}
                onClick={() => {
                  if (cancelPending.current) return
                  cancelPending.current = true
                  setConfirming(false)
                  if (cancelKey.current) cancellation.mutate(cancelKey.current)
                }}
              >
                Confirm cancellation
              </button>
            </div>
        </ModalDialog>
      ) : null}
    </section>
  )
}
