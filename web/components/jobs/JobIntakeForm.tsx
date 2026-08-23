"use client"
/* eslint-disable react-hooks/incompatible-library -- React Hook Form watch keeps reviewed text in its existing single source of truth. */

import { zodResolver } from "@hookform/resolvers/zod"
import { useRouter } from "next/navigation"
import { useState } from "react"
import { useForm } from "react-hook-form"

import { JobDescriptionUpload } from "@/components/jobs/JobDescriptionUpload"
import {
  reauthenticateExpiredSession,
  responseJson,
} from "@/lib/client-response"
import type {
  Client,
  Job,
  JobDescriptionExtraction,
  JobIntakeValues,
  ScorecardDraftResponse,
} from "@/lib/schemas"
import { jobIntakeSchema } from "@/lib/schemas"

type IntakeIntent = {
  fingerprint: string
  createKey: string
  generateKey: string
  job?: Job
}

type JobIntakeFormProps = {
  clients: readonly Client[]
  onDraftReady?: (draft: ScorecardDraftResponse) => void
}

function mutationKey(): string {
  return crypto.randomUUID()
}

export function JobIntakeForm({ clients, onDraftReady }: JobIntakeFormProps) {
  const [intent, setIntent] = useState<IntakeIntent | null>(null)
  const [extracting, setExtracting] = useState(false)
  const [extractedSource, setExtractedSource] = useState<string | null>(null)
  const router = useRouter()
  const [serverError, setServerError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    setError,
    setValue,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<JobIntakeValues>({
    resolver: zodResolver(jobIntakeSchema),
    defaultValues: {
      clientId: "",
      title: "",
      jobDescription: "",
      location: "",
      employmentModel: "",
    },
  })

  const submit = handleSubmit(async (values) => {
    setServerError(null)
    const fingerprint = JSON.stringify(values)
    let current = intent
    if (!current || current.fingerprint !== fingerprint) {
      current = {
        fingerprint,
        createKey: mutationKey(),
        generateKey: mutationKey(),
      }
      setIntent(current)
    }
    try {
      let job = current.job
      if (!job) {
        const response = await fetch("/api/bff/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": current.createKey,
          },
          body: JSON.stringify({
            client_id: values.clientId,
            title: values.title,
            job_description: values.jobDescription,
            location: values.location || null,
            employment_model: values.employmentModel || null,
          }),
        })
        job = await responseJson<Job>(response)
        current = { ...current, job }
        setIntent(current)
      }
      const generated = await responseJson<ScorecardDraftResponse>(
        await fetch(`/api/bff/jobs/${job.id}/scorecard/generate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": current.generateKey,
          },
          body: JSON.stringify({ expected_revision: job.draft_revision }),
        }),
      )
      if (onDraftReady) {
        onDraftReady(generated)
      } else {
        router.push(`/jobs/${job.id}/scorecard`)
      }
    } catch (error) {
      if (reauthenticateExpiredSession(error, router)) return
      const code = error instanceof Error ? error.message : "request_failed"
      if (code === "client_not_found" || (code === "job_not_found" && !current.job)) {
        setError("clientId", { message: "This client is no longer available" })
      } else if (code === "job_intake_invalid") {
        setError("jobDescription", { message: "Check the job details and try again" })
      } else {
        setServerError("We could not generate the scorecard. Try again safely.")
      }
    }
  })

  return (
    <form className="intake-form" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="client-id">Client</label>
        <select
          id="client-id"
          aria-invalid={Boolean(errors.clientId)}
          aria-describedby={errors.clientId ? "client-id-error" : undefined}
          {...register("clientId")}
        >
          <option value="">Choose an authorized client</option>
          {clients.map((client) => (
            <option key={client.id} value={client.id}>
              {client.name}
            </option>
          ))}
        </select>
        {errors.clientId ? <p id="client-id-error" className="field-error" role="alert">{errors.clientId.message}</p> : null}
      </div>

      <div className="field">
        <label htmlFor="job-title">Job title</label>
        <input
          id="job-title"
          aria-invalid={Boolean(errors.title)}
          aria-describedby={errors.title ? "job-title-error" : undefined}
          {...register("title")}
        />
        {errors.title ? <p id="job-title-error" className="field-error" role="alert">{errors.title.message}</p> : null}
      </div>

      <div className="field field-wide">
        <JobDescriptionUpload
          currentText={watch("jobDescription")}
          disabled={isSubmitting}
          onBusyChange={setExtracting}
          onExtracted={(result: JobDescriptionExtraction) => {
            setValue("jobDescription", result.text, {
              shouldDirty: true,
              shouldTouch: true,
              shouldValidate: true,
            })
            setExtractedSource(result.source.filename)
          }}
        />
        <label htmlFor="job-description">Job description</label>
        <textarea
          id="job-description"
          rows={12}
          aria-invalid={Boolean(errors.jobDescription)}
          aria-describedby={
            errors.jobDescription
              ? "job-description-hint job-description-error"
              : "job-description-hint"
          }
          {...register("jobDescription")}
        />
        <p id="job-description-hint" className="field-hint">
          Paste the licensed client brief. Candidate and provider data do not belong here.
        </p>
        {extractedSource ? <p className="extracted-source">Extracted from {extractedSource}</p> : null}
        {errors.jobDescription ? (
          <p id="job-description-error" className="field-error" role="alert">{errors.jobDescription.message}</p>
        ) : null}
      </div>

      <div className="field-row">
        <div className="field">
          <label htmlFor="job-location">Location</label>
          <input id="job-location" {...register("location")} />
        </div>
        <div className="field">
          <label htmlFor="employment-model">Employment model</label>
          <select id="employment-model" {...register("employmentModel")}>
            <option value="">Not specified</option>
            <option value="onsite">On-site</option>
            <option value="hybrid">Hybrid</option>
            <option value="remote">Remote</option>
          </select>
        </div>
      </div>

      {serverError ? (
        <p className="form-error" role="alert">
          {serverError}
        </p>
      ) : null}
      <button className="button button-primary" type="submit" disabled={isSubmitting || extracting}>
        {isSubmitting ? "Generating…" : "Generate scorecard"}
      </button>
    </form>
  )
}
