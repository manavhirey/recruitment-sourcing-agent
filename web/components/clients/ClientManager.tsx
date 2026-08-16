"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useRouter } from "next/navigation"
import { type ChangeEvent, useState } from "react"
import { useForm } from "react-hook-form"

import {
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import { industryTaxonomy } from "@/lib/generated-taxonomy"
import {
  clientCreateSchema,
  isManager,
  type Client,
  type ClientCreateValues,
  type Member,
  type Role,
} from "@/lib/schemas"

type ClientManagerProps = {
  clients: readonly Client[]
  members?: readonly GrantMember[]
  role: Role
}

type ClientAction = "industries" | "adjacency" | "grant"
type GrantMember = Pick<
  Member,
  "active" | "allowed_client_ids" | "display_name" | "membership_id" | "role"
>

function taxonomyLabel(code: string): string {
  return industryTaxonomy.industries.find((industry) => industry.code === code)?.label ?? code
}

function adjacentCodes(industryCode: string): readonly string[] {
  return industryTaxonomy.industries.find((industry) => industry.code === industryCode)
    ?.default_adjacency ?? []
}

export function ClientManager({
  clients: initialClients,
  members = [],
  role,
}: ClientManagerProps) {
  const router = useRouter()
  const [clients, setClients] = useState([...initialClients])
  const [error, setErrorMessage] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [intentKeys] = useState(() => new Map<string, string>())
  const [industrySelections, setIndustrySelections] = useState<Record<string, string[]>>(
    () => Object.fromEntries(initialClients.map((client) => [client.id, [...client.industry_codes]])),
  )
  const [adjacencySelections, setAdjacencySelections] = useState<
    Record<string, { source: string; target: string }>
  >(() =>
    Object.fromEntries(
      initialClients.map((client) => {
        const source = client.industry_codes[0] ?? ""
        return [client.id, { source, target: adjacentCodes(source)[0] ?? "" }]
      }),
    ),
  )
  const [grantSelections, setGrantSelections] = useState<Record<string, string>>({})
  const canManage = isManager(role)
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<ClientCreateValues>({
    resolver: zodResolver(clientCreateSchema),
    defaultValues: { name: "", industryCode: "" },
  })

  const createClient = handleSubmit(async (values) => {
    setErrorMessage(null)
    setStatus(null)
    const fingerprint = `create:${JSON.stringify(values)}`
    const idempotencyKey = intentKeys.get(fingerprint) ?? crypto.randomUUID()
    intentKeys.set(fingerprint, idempotencyKey)
    try {
      const response = await fetch("/api/bff/clients", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({
          name: values.name,
          industry_codes: [values.industryCode],
        }),
      })
      const client = await responseJson<Client>(response)
      intentKeys.delete(fingerprint)
      setClients((current) => [...current, client].sort((a, b) => a.name.localeCompare(b.name)))
      setIndustrySelections((current) => ({ ...current, [client.id]: [...client.industry_codes] }))
      const source = client.industry_codes[0] ?? ""
      setAdjacencySelections((current) => ({
        ...current,
        [client.id]: { source, target: adjacentCodes(source)[0] ?? "" },
      }))
      setStatus("Client created.")
      reset()
    } catch (error) {
      if (reauthenticateExpiredSession(error, router)) return
      setErrorMessage("The client could not be added. Check the details and try again.")
    }
  })

  async function clientMutation(
    client: Client,
    action: ClientAction,
    method: "POST" | "PUT",
    body: Record<string, unknown>,
  ): Promise<Client | null> {
    setErrorMessage(null)
    setStatus(null)
    const fingerprint = `${action}:${client.id}:${JSON.stringify(body)}`
    const idempotencyKey = intentKeys.get(fingerprint) ?? crypto.randomUUID()
    intentKeys.set(fingerprint, idempotencyKey)
    setBusyAction(fingerprint)
    try {
      const actionPath = {
        adjacency: "adjacent-industries",
        grant: "grants",
        industries: "industries",
      }[action]
      const response = await fetch(`/api/bff/clients/${client.id}/${actionPath}`, {
        method,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(body),
      })
      if (action === "grant") {
        await responseJson<unknown>(response)
        intentKeys.delete(fingerprint)
        setStatus("Recruiter access granted.")
        return null
      }
      const updated = await responseJson<Client>(response)
      intentKeys.delete(fingerprint)
      setClients((current) => current.map((item) => item.id === updated.id ? updated : item))
      setStatus(action === "industries" ? "Industries updated." : "Adjacent industry approved.")
      return updated
    } catch (error) {
      if (reauthenticateExpiredSession(error, router)) return null
      setErrorMessage(
        action === "grant"
          ? "Recruiter access could not be granted. Try again safely."
          : "The client could not be updated. Try again safely.",
      )
      return null
    } finally {
      setBusyAction(null)
    }
  }

  function updateIndustrySelection(clientId: string, event: ChangeEvent<HTMLSelectElement>) {
    const values = Array.from(event.currentTarget.selectedOptions, (option) => option.value)
    setIndustrySelections((current) => ({ ...current, [clientId]: values }))
  }

  return (
    <div className="client-manager">
      {canManage ? (
        <details className="create-panel">
          <summary className="button button-secondary">Add client</summary>
          <form onSubmit={createClient} noValidate>
            <div className="field">
              <label htmlFor="client-name">Client name</label>
              <input
                id="client-name"
                aria-invalid={Boolean(errors.name)}
                aria-describedby={errors.name ? "client-name-error" : undefined}
                {...register("name")}
              />
              {errors.name ? <p id="client-name-error" className="field-error" role="alert">{errors.name.message}</p> : null}
            </div>
            <div className="field">
              <label htmlFor="client-industry">Primary industry</label>
              <select
                id="client-industry"
                aria-invalid={Boolean(errors.industryCode)}
                aria-describedby={errors.industryCode ? "client-industry-error" : undefined}
                {...register("industryCode")}
              >
                <option value="">Choose from the controlled taxonomy</option>
                {industryTaxonomy.industries.map((industry) => (
                  <option key={industry.code} value={industry.code}>
                    {industry.label}
                  </option>
                ))}
              </select>
              {errors.industryCode ? (
                <p id="client-industry-error" className="field-error" role="alert">{errors.industryCode.message}</p>
              ) : null}
            </div>
            <button className="button button-primary" type="submit" disabled={isSubmitting}>
              Create client
            </button>
          </form>
        </details>
      ) : null}

      {error ? <p role="alert" className="form-error">{error}</p> : null}
      <p className="sr-only" role="status" aria-live="polite">{status}</p>
      {clients.length === 0 ? (
        <div className="empty-state">
          <h2>No authorized clients</h2>
          <p>{canManage ? "Add the first client to begin intake." : "Ask an agency manager for client access."}</p>
        </div>
      ) : (
        <ul className="client-grid" aria-label="Authorized clients">
          {clients.map((client) => (
            <li className="client-card" key={client.id}>
              <div>
                <p className="eyebrow">Client</p>
                <h2>{client.name}</h2>
                <p>{client.industry_codes.map(taxonomyLabel).join(", ") || "Industry not assigned"}</p>
              </div>
              {canManage ? (
                <details className="client-controls">
                  <summary className="button button-quiet">Manage {client.name}</summary>
                  <div className="client-controls-grid">
                    <form
                      onSubmit={async (event) => {
                        event.preventDefault()
                        const selection = industrySelections[client.id] ?? []
                        if (selection.length === 0) return
                        const updated = await clientMutation(
                          client,
                          "industries",
                          "PUT",
                          { industry_codes: selection },
                        )
                        if (updated) {
                          const source = updated.industry_codes[0] ?? ""
                          setAdjacencySelections((current) => ({
                            ...current,
                            [client.id]: { source, target: adjacentCodes(source)[0] ?? "" },
                          }))
                        }
                      }}
                    >
                      <label htmlFor={`industries-${client.id}`}>Primary industries for {client.name}</label>
                      <select
                        id={`industries-${client.id}`}
                        multiple
                        value={industrySelections[client.id] ?? []}
                        onChange={(event) => updateIndustrySelection(client.id, event)}
                      >
                        {industryTaxonomy.industries.map((industry) => (
                          <option key={industry.code} value={industry.code}>{industry.label}</option>
                        ))}
                      </select>
                      <button
                        className="button button-secondary"
                        type="submit"
                        disabled={Boolean(busyAction) || (industrySelections[client.id]?.length ?? 0) === 0}
                      >
                        Update industries
                      </button>
                    </form>

                    <form
                      onSubmit={async (event) => {
                        event.preventDefault()
                        const selection = adjacencySelections[client.id]
                        if (!selection?.source || !selection.target) return
                        await clientMutation(client, "adjacency", "PUT", {
                          industry_code: selection.source,
                          adjacent_industry_code: selection.target,
                        })
                      }}
                    >
                      <label htmlFor={`adjacency-source-${client.id}`}>Industry for adjacency</label>
                      <select
                        id={`adjacency-source-${client.id}`}
                        value={adjacencySelections[client.id]?.source ?? ""}
                        onChange={(event) => {
                          const source = event.currentTarget.value
                          setAdjacencySelections((current) => ({
                            ...current,
                            [client.id]: { source, target: adjacentCodes(source)[0] ?? "" },
                          }))
                        }}
                      >
                        {(industrySelections[client.id] ?? []).map((code) => (
                          <option key={code} value={code}>{taxonomyLabel(code)}</option>
                        ))}
                      </select>
                      <label htmlFor={`adjacency-target-${client.id}`}>Approved adjacent industry</label>
                      <select
                        id={`adjacency-target-${client.id}`}
                        value={adjacencySelections[client.id]?.target ?? ""}
                        onChange={(event) => setAdjacencySelections((current) => ({
                          ...current,
                          [client.id]: {
                            source: current[client.id]?.source ?? "",
                            target: event.currentTarget.value,
                          },
                        }))}
                      >
                        <option value="">No controlled adjacency available</option>
                        {adjacentCodes(adjacencySelections[client.id]?.source ?? "").map((code) => (
                          <option key={code} value={code}>{taxonomyLabel(code)}</option>
                        ))}
                      </select>
                      <button
                        className="button button-secondary"
                        type="submit"
                        disabled={Boolean(busyAction) || !adjacencySelections[client.id]?.target}
                      >
                        Approve adjacency
                      </button>
                    </form>

                    <form
                      onSubmit={async (event) => {
                        event.preventDefault()
                        const membershipId = grantSelections[client.id]
                        if (!membershipId) return
                        await clientMutation(client, "grant", "POST", { membership_id: membershipId })
                      }}
                    >
                      <label htmlFor={`grant-${client.id}`}>Recruiter access for {client.name}</label>
                      <select
                        id={`grant-${client.id}`}
                        value={grantSelections[client.id] ?? ""}
                        onChange={(event) => {
                          const membershipId = event.currentTarget.value
                          setGrantSelections((current) => ({
                            ...current,
                            [client.id]: membershipId,
                          }))
                        }}
                      >
                        <option value="">Choose an active recruiter</option>
                        {members
                          .filter((member) =>
                            member.active &&
                            member.role === "recruiter" &&
                            member.allowed_client_ids !== null &&
                            !member.allowed_client_ids.includes(client.id),
                          )
                          .map((member) => (
                            <option key={member.membership_id} value={member.membership_id}>
                              {member.display_name}
                            </option>
                          ))}
                      </select>
                      <button
                        className="button button-secondary"
                        type="submit"
                        disabled={Boolean(busyAction) || !grantSelections[client.id]}
                      >
                        Grant recruiter access
                      </button>
                    </form>
                  </div>
                </details>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
