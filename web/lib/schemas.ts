import { z } from "zod"

import type { components } from "@/lib/generated-api"

export type Role = components["schemas"]["Role"]
export type Me = components["schemas"]["MeResponse"]
export type Member = components["schemas"]["MemberResponse"]
export type Client = components["schemas"]["ClientResponse"]
export type Job = components["schemas"]["JobResponse"]
export type JobSummary = components["schemas"]["JobSummary"]
export type JobPage = components["schemas"]["JobPage"]
export type ScorecardDraftResponse =
  components["schemas"]["ScorecardDraftResponse"]
export type JobDescriptionExtraction =
  components["schemas"]["JobDescriptionExtractionResponse"]
export type SeniorityLevel = components["schemas"]["SeniorityLevel"]
export type SeniorityOption = components["schemas"]["SeniorityOption"]
export type ScorecardDraft = components["schemas"]["ScorecardDraft"]
export type ScorecardCriterion = components["schemas"]["ScorecardCriterion"]
export type ConfirmedScorecard = components["schemas"]["ConfirmedScorecard"]
export type SourcingRun = components["schemas"]["RunResponse"]
export type JobCandidate = components["schemas"]["JobCandidateView"]
export type JobCandidatePage = components["schemas"]["JobCandidatePage"]
export type CandidateDirectoryPage = components["schemas"]["CandidateDirectoryPage"]
export type CandidateJob = components["schemas"]["CandidateJobView"]
export type CandidateActivityPage = components["schemas"]["ActivityPage"]
export type RunActivity = components["schemas"]["RunActivityResponse"]
export type Notification = components["schemas"]["NotificationResponse"]
export type Invitation = components["schemas"]["InvitationResponse"]
export type MaskedContact = components["schemas"]["MaskedContact"]

export const jobIntakeSchema = z.object({
  clientId: z.uuid({ error: "Select a client" }),
  title: z.string().trim().min(1, "Enter a job title").max(255),
  jobDescription: z
    .string()
    .trim()
    .min(1, "Enter a job description")
    .max(50_000),
  location: z.string().trim().max(255),
  employmentModel: z.enum(["", "onsite", "hybrid", "remote"]),
})

export type JobIntakeValues = z.infer<typeof jobIntakeSchema>

export const clientCreateSchema = z.object({
  name: z.string().trim().min(1, "Enter a client name").max(255),
  industryCode: z.string().min(1, "Choose a primary industry"),
})

export type ClientCreateValues = z.infer<typeof clientCreateSchema>

export function isManager(role: Role): boolean {
  return role === "owner" || role === "admin"
}
