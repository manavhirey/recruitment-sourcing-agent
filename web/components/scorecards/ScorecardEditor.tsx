"use client"

import { useMemo, useRef, useState } from "react"
import { useRouter } from "next/navigation"

import {
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import { industryTaxonomy } from "@/lib/generated-taxonomy"
import {
  adjacentConfirmationId,
  criterionConfirmationId,
  requiredInferenceIds,
  uncertaintyConfirmationId,
} from "@/lib/inference-confirmations"
import type {
  ConfirmedScorecard,
  ScorecardCriterion,
  ScorecardDraft,
  ScorecardDraftResponse,
  SeniorityLevel,
  SourcingRun,
} from "@/lib/schemas"

type ScorecardEditorProps = {
  draft: Omit<ScorecardDraftResponse, "original_job_description">
  allowedIndustryCodes: readonly string[]
  alreadyConfirmed?: boolean
  onStarted?: (run: SourcingRun) => void
}

type ScorecardEditorDraft = Omit<ScorecardDraft, "seniority"> & {
  seniority: string[]
}

type ScorecardIntent = {
  fingerprint: string
  saveKey: string
  confirmKey: string
  sourceKey: string
  revision: number
  stage: "save" | "confirm" | "source"
}

type SuggestedItem = {
  id: string
  label: string
  kind: "criterion" | "adjacent" | "uncertainty"
}

const existingRunCodes = new Set(["active_run_exists", "scorecard_run_exists"])

function newCriterion(kind: ScorecardCriterion["kind"], position: number): ScorecardCriterion {
  return {
    key: `manual_${kind}_${position + 1}`,
    label: "New job-related criterion",
    kind,
    evidence_required: false,
    source_text: null,
    inferred: false,
    recruiter_entered: true,
    lawful_requirement_confirmed: kind !== "exclusion",
  }
}

function listValues(value: string, separator: string): string[] {
  return value
    .split(separator)
    .map((item) => item.trim())
    .filter(Boolean)
}

function isValidExperienceBound(value: number | null): boolean {
  return value === null || (Number.isInteger(value) && value >= 0 && value <= 50)
}

export function ScorecardEditor({
  draft: response,
  allowedIndustryCodes,
  alreadyConfirmed = false,
  onStarted,
}: ScorecardEditorProps) {
  const router = useRouter()
  const [draft, setDraft] = useState<ScorecardEditorDraft>(() => ({
    target_titles: [...(response.draft.target_titles ?? [])],
    criteria: (response.draft.criteria ?? []).map((criterion) => ({ ...criterion })),
    seniority: [...(response.draft.seniority ?? [])],
    minimum_years: response.draft.minimum_years ?? null,
    maximum_years: response.draft.maximum_years ?? null,
    locations: [...(response.draft.locations ?? [])],
    industry_code: response.draft.industry_code ?? "",
    suggested_adjacent_industries: [...(response.draft.suggested_adjacent_industries ?? [])],
    uncertainties: [...(response.draft.uncertainties ?? [])],
    confirmed_inferred_items: [...(response.draft.confirmed_inferred_items ?? [])],
  }))
  const [targetTitlesInput, setTargetTitlesInput] = useState(
    () => (response.draft.target_titles ?? []).join(", "),
  )
  const [customEnabled, setCustomEnabled] = useState(
    () => response.draft.minimum_years != null || response.draft.maximum_years != null,
  )
  const [locationsInput, setLocationsInput] = useState(
    () => (response.draft.locations ?? []).join("\n"),
  )
  const [confirmedSuggestions, setConfirmedSuggestions] = useState<Set<string>>(
    () => {
      const required = new Set(requiredInferenceIds({
        criteria: response.draft.criteria ?? [],
        suggested_adjacent_industries:
          response.draft.suggested_adjacent_industries ?? [],
        uncertainties: response.draft.uncertainties ?? [],
      }))
      return new Set(
        (response.draft.confirmed_inferred_items ?? []).filter((id) => required.has(id)),
      )
    },
  )
  const [error, setError] = useState<string | null>(null)
  const [industryError, setIndustryError] = useState<string | null>(null)
  const [adjacencyError, setAdjacencyError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const intent = useRef<ScorecardIntent | null>(null)
  const confirmedSourceKey = useRef<string | null>(null)
  const inFlight = useRef(false)

  const suggestions = useMemo<SuggestedItem[]>(() => [
    ...draft.criteria
      .filter((criterion) => criterion.inferred)
      .map((criterion) => ({
        id: criterionConfirmationId(criterion),
        label: criterion.label,
        kind: "criterion" as const,
      })),
    ...draft.suggested_adjacent_industries.map((code) => ({
      id: adjacentConfirmationId(code),
      label: code,
      kind: "adjacent" as const,
    })),
    ...draft.uncertainties.map((uncertainty, index) => ({
      id: uncertaintyConfirmationId(uncertainty, index),
      label: uncertainty,
      kind: "uncertainty" as const,
    })),
  ], [draft])
  const confirmedInferenceIds = suggestions
    .map((item) => item.id)
    .filter((id) => confirmedSuggestions.has(id))
    .sort()
  const allSuggestionsResolved = suggestions.every((item) => confirmedSuggestions.has(item.id))
  const canonicalSeniority = new Set(
    response.seniority_options.map((option) => option.value),
  )
  const unrecognizedSeniority = draft.seniority.filter(
    (value) => !canonicalSeniority.has(value as SeniorityLevel),
  )
  const minimumYears = draft.minimum_years ?? null
  const maximumYears = draft.maximum_years ?? null
  const customBoundsPresent = minimumYears !== null || maximumYears !== null
  const minimumYearsValid = isValidExperienceBound(minimumYears)
  const maximumYearsValid = isValidExperienceBound(maximumYears)
  const yearsOrdered =
    minimumYears === null ||
    maximumYears === null ||
    minimumYears <= maximumYears
  const yearsValid = minimumYearsValid && maximumYearsValid && yearsOrdered
  const structurallyValid =
    draft.target_titles.some((title) => title.trim()) &&
    draft.criteria.length > 0 &&
    draft.criteria.every((criterion) =>
      criterion.kind !== "exclusion" ||
      Boolean(criterion.source_text) ||
      (criterion.recruiter_entered && criterion.lawful_requirement_confirmed),
    ) &&
    Boolean(draft.industry_code) &&
    unrecognizedSeniority.length === 0 &&
    (!customEnabled || customBoundsPresent) &&
    yearsValid

  function updateCriterion(index: number, patch: Partial<ScorecardCriterion>) {
    setDraft((current) => ({
      ...current,
      criteria: current.criteria.map((criterion, criterionIndex) =>
        criterionIndex === index ? { ...criterion, ...patch } : criterion,
      ),
    }))
    intent.current = null
  }

  function removeSuggestion(item: SuggestedItem) {
    setDraft((current) => {
      if (item.kind === "criterion") {
        return {
          ...current,
          criteria: current.criteria.filter(
            (criterion) => criterionConfirmationId(criterion) !== item.id,
          ),
        }
      }
      if (item.kind === "adjacent") {
        return {
          ...current,
          suggested_adjacent_industries: current.suggested_adjacent_industries.filter(
            (code) => adjacentConfirmationId(code) !== item.id,
          ),
        }
      }
      return {
        ...current,
        uncertainties: current.uncertainties.filter(
          (uncertainty, index) =>
            uncertaintyConfirmationId(uncertainty, index) !== item.id,
        ),
      }
    })
    setConfirmedSuggestions((current) => {
      const next = new Set(current)
      next.delete(item.id)
      return next
    })
    intent.current = null
  }

  function setSuggestionConfirmed(id: string, checked: boolean) {
    setConfirmedSuggestions((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
    intent.current = null
  }

  function openJobWorkspace() {
    router.push(`/jobs/${response.job_id}`)
  }

  async function confirmAndSource() {
    if (inFlight.current || !allSuggestionsResolved || !structurallyValid) return
    inFlight.current = true
    setSubmitting(true)
    setError(null)
    setIndustryError(null)
    setAdjacencyError(null)
    const mutationDraft: ScorecardEditorDraft = {
      ...draft,
      seniority: response.seniority_options
        .map((option) => option.value)
        .filter((value) => draft.seniority.includes(value)),
      confirmed_inferred_items: confirmedInferenceIds,
    }
    const fingerprint = JSON.stringify(mutationDraft)
    if (!intent.current || intent.current.fingerprint !== fingerprint) {
      intent.current = {
        fingerprint,
        saveKey: crypto.randomUUID(),
        confirmKey: crypto.randomUUID(),
        sourceKey: crypto.randomUUID(),
        revision: response.draft_revision,
        stage: "save",
      }
    }
    const current = intent.current
    try {
      if (current.stage === "save") {
        const saved = await responseJson<ScorecardDraftResponse>(
          await fetch(`/api/bff/jobs/${response.job_id}/scorecard/draft`, {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": current.saveKey,
            },
            body: JSON.stringify({
              expected_revision: current.revision,
              draft: mutationDraft,
            }),
          }),
        )
        current.revision = saved.draft_revision
        current.stage = "confirm"
      }
      if (current.stage === "confirm") {
        await responseJson<ConfirmedScorecard>(
          await fetch(`/api/bff/jobs/${response.job_id}/scorecard/confirm`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": current.confirmKey,
            },
            body: JSON.stringify({ expected_revision: current.revision }),
          }),
        )
        current.stage = "source"
      }
      const run = await responseJson<SourcingRun>(
        await fetch(`/api/bff/jobs/${response.job_id}/runs`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": current.sourceKey,
          },
          body: "{}",
        }),
      )
      if (onStarted) onStarted(run)
      else openJobWorkspace()
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return
      const code = caught instanceof Error ? caught.message : "request_failed"
      if (code === "scorecard_industry_invalid") {
        setIndustryError("Choose an industry assigned to this client.")
      } else if (code === "scorecard_adjacency_not_approved") {
        setAdjacencyError("Delete adjacent industries that are not approved for this client.")
      } else if (existingRunCodes.has(code)) {
        openJobWorkspace()
      } else {
        setError(
          code === "scorecard_revision_conflict"
            ? "The scorecard changed elsewhere. Reload before confirming."
            : code === "scorecard_inferences_unresolved"
              ? "Review every suggested item again before confirming."
              : "The scorecard was not started. Retry uses the same safe request.",
        )
      }
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  async function sourceConfirmedVersion() {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setError(null)
    confirmedSourceKey.current ??= crypto.randomUUID()
    try {
      const run = await responseJson<SourcingRun>(
        await fetch(`/api/bff/jobs/${response.job_id}/runs`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": confirmedSourceKey.current,
          },
          body: "{}",
        }),
      )
      if (onStarted) onStarted(run)
      else openJobWorkspace()
    } catch (caught) {
      if (reauthenticateExpiredSession(caught, router)) return
      const code = caught instanceof Error ? caught.message : "request_failed"
      if (existingRunCodes.has(code)) {
        openJobWorkspace()
      } else {
        setError("Sourcing was not started. Retry uses the same safe request.")
      }
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  if (alreadyConfirmed) {
    return (
      <div className="scorecard-editor confirmed-scorecard-state">
        <div className="empty-state">
          <p className="eyebrow">Confirmed scorecard</p>
          <h2>This immutable version is ready to source.</h2>
          <p>Starting here cannot create another scorecard version.</p>
        </div>
        {error ? <p role="alert" className="form-error">{error}</p> : null}
        <div className="sticky-action">
          <p>The confirmed criteria will be used as saved.</p>
          <button
            className="button button-primary"
            type="button"
            disabled={submitting}
            onClick={sourceConfirmedVersion}
          >
            {submitting ? "Starting…" : "Start sourcing"}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="scorecard-editor">
      {response.extraction_status === "manual_required" ? (
        <div className="manual-warning" role="alert">
          <strong>Manual scorecard required.</strong>{" "}
          {response.extraction_warning ?? "Automated extraction could not be completed."}
        </div>
      ) : null}

      <fieldset className="scorecard-fields" disabled={submitting}>
      <legend className="sr-only">Scorecard criteria</legend>
      <div className="provenance-legend" aria-label="Criterion provenance">
        <span className="provenance provenance-extracted">From job description</span>
        <span className="provenance provenance-inferred">Suggested — confirm before use</span>
        <span className="provenance provenance-manual">Recruiter entered</span>
      </div>

      <section className="scorecard-section">
        <h2>Role profile</h2>
        <div className="field">
          <label htmlFor="target-titles">Target titles</label>
          <input
            id="target-titles"
            value={targetTitlesInput}
            onChange={(event) => {
              setTargetTitlesInput(event.target.value)
              setDraft((current) => ({
                ...current,
                target_titles: listValues(event.target.value, ","),
              }))
              intent.current = null
            }}
            placeholder="Senior Product Manager, Product Lead"
          />
        </div>
        <div className="field">
          <label htmlFor="primary-industry">Primary industry</label>
          <select
            id="primary-industry"
            value={draft.industry_code}
            aria-invalid={industryError ? true : undefined}
            aria-describedby={industryError ? "primary-industry-error" : undefined}
            onChange={(event) => {
              setDraft((current) => ({ ...current, industry_code: event.target.value }))
              setIndustryError(null)
              intent.current = null
            }}
          >
            <option value="">Choose an industry</option>
            {industryTaxonomy.industries
              .filter((industry) => allowedIndustryCodes.includes(industry.code))
              .map((industry) => (
              <option key={industry.code} value={industry.code}>{industry.label}</option>
              ))}
          </select>
          {industryError ? (
            <p id="primary-industry-error" className="field-error" role="alert">
              {industryError}
            </p>
          ) : null}
        </div>
        <div className="field-row role-constraints">
          <fieldset className="field seniority-controls">
            <legend>Seniority requirements</legend>
            <div className="seniority-options">
              {response.seniority_options.map((option) => {
                const checked = draft.seniority.includes(option.value)
                const range = option.maximum_years === null
                  ? `${option.minimum_years}+ years`
                  : `${option.minimum_years}–${option.maximum_years} years`
                return (
                  <label className="check-row" key={option.value}>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={submitting || customEnabled}
                      onChange={() => {
                        setDraft((current) => ({
                          ...current,
                          seniority: checked
                            ? current.seniority.filter((value) => value !== option.value)
                            : [...current.seniority, option.value],
                        }))
                        intent.current = null
                      }}
                    />
                    {option.label} — {range}
                  </label>
                )
              })}
            </div>
            {unrecognizedSeniority.map((value) => (
              <div className="field-error legacy-seniority" key={value} role="alert">
                <span>Unrecognized historical seniority: {value}</span>
                <button
                  className="button button-danger-quiet"
                  type="button"
                  disabled={submitting}
                  onClick={() => {
                    setDraft((current) => ({
                      ...current,
                      seniority: current.seniority.filter((item) => item !== value),
                    }))
                    intent.current = null
                  }}
                >
                  Remove {value}
                </button>
              </div>
            ))}
            <label className="check-row custom-range-toggle">
              <input
                type="checkbox"
                checked={customEnabled}
                disabled={submitting}
                onChange={(event) => {
                  if (event.target.checked) {
                    setCustomEnabled(true)
                  } else {
                    setCustomEnabled(false)
                    setDraft((current) => ({
                      ...current,
                      minimum_years: null,
                      maximum_years: null,
                    }))
                  }
                  intent.current = null
                }}
              />
              Use custom experience range
            </label>
            {customEnabled ? (
              <p className="field-hint" role="status">
                This custom range overrides the selected seniority levels.
              </p>
            ) : null}
          </fieldset>
          <div className="field">
            <label htmlFor="locations">Locations</label>
            <textarea
              id="locations"
              rows={2}
              value={locationsInput}
              onChange={(event) => {
                setLocationsInput(event.target.value)
                setDraft((current) => ({
                  ...current,
                  locations: listValues(event.target.value, "\n"),
                }))
                intent.current = null
              }}
              placeholder={"New York, NY\nRemote"}
            />
          </div>
        </div>
        {customEnabled ? (
          <>
            <div className="field-row">
              <div className="field">
                <label htmlFor="minimum-years">Minimum years</label>
                <input
                  id="minimum-years"
                  type="number"
                  min={0}
                  max={50}
                  value={draft.minimum_years ?? ""}
                  aria-invalid={!minimumYearsValid || !yearsOrdered || undefined}
                  aria-describedby={
                    !minimumYearsValid
                      ? "minimum-years-error"
                      : !yearsOrdered
                        ? "years-error"
                        : undefined
                  }
                  onChange={(event) => {
                    setDraft((current) => ({
                      ...current,
                      minimum_years: event.target.value === ""
                        ? null
                        : Number(event.target.value),
                    }))
                    intent.current = null
                  }}
                />
              </div>
              <div className="field">
                <label htmlFor="maximum-years">Maximum years</label>
                <input
                  id="maximum-years"
                  type="number"
                  min={0}
                  max={50}
                  value={draft.maximum_years ?? ""}
                  aria-invalid={!maximumYearsValid || !yearsOrdered || undefined}
                  aria-describedby={
                    !maximumYearsValid
                      ? "maximum-years-error"
                      : !yearsOrdered
                        ? "years-error"
                        : undefined
                  }
                  onChange={(event) => {
                    setDraft((current) => ({
                      ...current,
                      maximum_years: event.target.value === ""
                        ? null
                        : Number(event.target.value),
                    }))
                    intent.current = null
                  }}
                />
              </div>
            </div>
            {!customBoundsPresent ? (
              <p className="field-error" role="alert">
                Enter a minimum or maximum year.
              </p>
            ) : null}
            {!minimumYearsValid ? (
              <p id="minimum-years-error" className="field-error" role="alert">
                Minimum years must be a whole number from 0 to 50.
              </p>
            ) : null}
            {!maximumYearsValid ? (
              <p id="maximum-years-error" className="field-error" role="alert">
                Maximum years must be a whole number from 0 to 50.
              </p>
            ) : null}
            {!yearsOrdered ? (
              <p id="years-error" className="field-error" role="alert">
                Maximum years cannot be less than minimum years.
              </p>
            ) : null}
          </>
        ) : null}
      </section>

      {(["must_have", "preference", "exclusion"] as const).map((kind) => (
        <section className="scorecard-section" key={kind}>
          <div className="section-heading">
            <h2>{kind === "must_have" ? "Must-haves" : kind === "preference" ? "Preferences" : "Exclusions"}</h2>
            <button
              type="button"
              className="button button-quiet"
              onClick={() => setDraft((current) => ({
                ...current,
                criteria: [...current.criteria, newCriterion(kind, current.criteria.length)],
              }))}
            >
              Add {kind === "must_have" ? "must-have" : kind}
            </button>
          </div>
          <ul className="criteria-list">
            {draft.criteria.map((criterion, index) =>
              criterion.kind === kind ? (
                <li key={criterion.key} className="criterion-row">
                  <span className={`provenance ${criterion.inferred ? "provenance-inferred" : criterion.recruiter_entered ? "provenance-manual" : "provenance-extracted"}`}>
                    {criterion.inferred ? "Suggested" : criterion.recruiter_entered ? "Recruiter entered" : "Extracted"}
                  </span>
                  <label className="sr-only" htmlFor={`criterion-${criterion.key}`}>Criterion</label>
                  <input
                    id={`criterion-${criterion.key}`}
                    value={criterion.label}
                    onChange={(event) => updateCriterion(index, { label: event.target.value })}
                  />
                  {kind === "exclusion" && !criterion.source_text ? (
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={criterion.lawful_requirement_confirmed}
                        onChange={(event) => updateCriterion(index, {
                          recruiter_entered: true,
                          lawful_requirement_confirmed: event.target.checked,
                        })}
                      />
                      Confirm this is a lawful, job-related requirement
                    </label>
                  ) : null}
                </li>
              ) : null,
            )}
          </ul>
        </section>
      ))}

      <section className="scorecard-section">
        <h2>Adjacent industries</h2>
        <ul className="suggestion-list">
          {suggestions.filter((item) => item.kind === "adjacent").map((item) => (
            <SuggestionRow key={item.id} item={item} confirmed={confirmedSuggestions.has(item.id)} onConfirm={(checked) => {
              setSuggestionConfirmed(item.id, checked)
            }} onDelete={() => removeSuggestion(item)} />
          ))}
        </ul>
        {adjacencyError ? <p className="field-error" role="alert">{adjacencyError}</p> : null}
      </section>

      <section className="scorecard-section">
        <h2>Uncertainties</h2>
        <ul className="suggestion-list">
          {suggestions.filter((item) => item.kind === "uncertainty").map((item) => (
            <SuggestionRow key={item.id} item={item} confirmed={confirmedSuggestions.has(item.id)} onConfirm={(checked) => {
              setSuggestionConfirmed(item.id, checked)
            }} onDelete={() => removeSuggestion(item)} />
          ))}
        </ul>
      </section>

      {suggestions.filter((item) => item.kind === "criterion").length > 0 ? (
        <section className="scorecard-section inferred-review">
          <h2>Suggested criteria awaiting review</h2>
          <ul className="suggestion-list">
            {suggestions.filter((item) => item.kind === "criterion").map((item) => (
              <SuggestionRow key={item.id} item={item} confirmed={confirmedSuggestions.has(item.id)} onConfirm={(checked) => {
                setSuggestionConfirmed(item.id, checked)
              }} onDelete={() => removeSuggestion(item)} />
            ))}
          </ul>
        </section>
      ) : null}
      </fieldset>

      {error ? <p role="alert" className="form-error">{error}</p> : null}
      <div className="sticky-action">
        <p>{allSuggestionsResolved ? "All suggestions reviewed." : `${suggestions.filter((item) => !confirmedSuggestions.has(item.id)).length} suggestions need review.`}</p>
        <button
          className="button button-primary"
          type="button"
          disabled={!allSuggestionsResolved || !structurallyValid || submitting}
          onClick={confirmAndSource}
        >
          {submitting ? "Confirming…" : "Confirm and source"}
        </button>
      </div>
    </div>
  )
}

function SuggestionRow({
  item,
  confirmed,
  onConfirm,
  onDelete,
}: {
  item: SuggestedItem
  confirmed: boolean
  onConfirm: (checked: boolean) => void
  onDelete: () => void
}) {
  return (
    <li className="suggestion-row">
      <label className="check-row">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => onConfirm(event.target.checked)}
        />
        Confirm suggested {item.label}
      </label>
      <button className="button button-danger-quiet" type="button" onClick={onDelete}>
        Delete {item.label}
      </button>
    </li>
  )
}
