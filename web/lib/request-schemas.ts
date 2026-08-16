import { z } from "zod"

export const clientCreateRequest = z
  .object({
    name: z.string().trim().min(1).max(255),
    industry_codes: z.array(z.string().min(1).max(128)).max(15),
  })
  .strict()

export const clientIndustriesRequest = z
  .object({ industry_codes: z.array(z.string().min(1).max(128)).max(15) })
  .strict()

export const clientAdjacencyRequest = z
  .object({
    industry_code: z.string().min(1).max(128),
    adjacent_industry_code: z.string().min(1).max(128),
  })
  .strict()

export const clientGrantRequest = z.object({ membership_id: z.uuid() }).strict()

export const jobCreateRequest = z
  .object({
    client_id: z.uuid(),
    title: z.string().trim().min(1).max(255),
    job_description: z.string().trim().min(1).max(50_000),
    location: z.string().trim().max(255).nullable(),
    employment_model: z.string().trim().max(64).nullable(),
  })
  .strict()

export const revisionRequest = z.object({ expected_revision: z.number().int().min(0) }).strict()

const scorecardCriterionRequest = z
  .object({
    key: z.string().regex(/^[a-z][a-z0-9_]{1,63}$/),
    label: z.string().trim().min(3).max(160),
    kind: z.enum(["must_have", "preference", "exclusion"]),
    evidence_required: z.boolean(),
    source_text: z.string().trim().min(1).max(500).nullable(),
    inferred: z.boolean(),
    recruiter_entered: z.boolean(),
    lawful_requirement_confirmed: z.boolean(),
  })
  .strict()

export const scorecardDraftRequest = z
  .object({
    target_titles: z.array(z.string().trim().min(1)).min(1).max(12),
    criteria: z.array(scorecardCriterionRequest).min(1).max(40),
    seniority: z.array(z.string().trim().min(1)).max(8),
    minimum_years: z.number().int().min(0).max(50).nullable(),
    maximum_years: z.number().int().min(0).max(50).nullable(),
    locations: z.array(z.string().trim().min(1)).max(20),
    industry_code: z.string().trim().min(1).max(128),
    suggested_adjacent_industries: z.array(z.string().trim().min(1).max(128)).max(12),
    uncertainties: z.array(z.string().trim().min(1)).max(20),
    confirmed_inferred_items: z.array(z.string().min(1)).max(72),
  })
  .strict()
  .refine(
    (value) =>
      value.minimum_years === null ||
      value.maximum_years === null ||
      value.minimum_years <= value.maximum_years,
  )

export const scorecardDraftUpdateRequest = z
  .object({
    expected_revision: z.number().int().min(0),
    draft: scorecardDraftRequest,
  })
  .strict()

export const emptyObjectRequest = z.object({}).strict()
