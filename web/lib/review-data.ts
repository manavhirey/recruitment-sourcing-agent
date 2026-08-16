import { z } from "zod"

const criterionSchema = z.object({
  key: z.string(),
  label: z.string(),
  state: z.enum(["supported", "failed", "unknown"]),
  summary: z.string(),
  points: z.number().int().min(0).max(100),
  max_points: z.number().int().min(0).max(100),
  evidence: z.array(z.string()).default([]),
  source_refs: z.array(z.string()).default([]),
})

const evidenceSchema = z.object({
  total: z.number().int().min(0).max(100).optional(),
  breakdown: z.object({
    role_and_skills: z.number().int().min(0).max(35).optional(),
    scope_seniority_years: z.number().int().min(0).max(25).optional(),
    industry: z.number().int().min(0).max(20).optional(),
    location_and_eligibility: z.number().int().min(0).max(10).optional(),
    recency_and_trajectory: z.number().int().min(0).max(10).optional(),
  }).default({}),
  criteria: z.array(criterionSchema).default([]),
  failed_must_haves: z.array(z.string()).default([]),
  unknown_keys: z.array(z.string()).default([]),
})

export type MatchEvidence = z.infer<typeof evidenceSchema>

export function matchEvidence(value: unknown): MatchEvidence {
  const parsed = evidenceSchema.safeParse(value)
  return parsed.success
    ? parsed.data
    : { breakdown: {}, criteria: [], failed_must_haves: [], unknown_keys: [] }
}
