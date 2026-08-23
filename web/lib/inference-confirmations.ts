type InferredCriterion = {
  key: string
  label: string
  kind: string
  evidence_required: boolean
  source_text?: string | null
  inferred: boolean
  recruiter_entered: boolean
  lawful_requirement_confirmed: boolean
}

type InferenceDraft = {
  criteria: readonly InferredCriterion[]
  suggested_adjacent_industries: readonly string[]
  uncertainties: readonly string[]
}

function base64Url(value: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
  const bytes = new TextEncoder().encode(value)
  let result = ""
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index] ?? 0
    const second = bytes[index + 1]
    const third = bytes[index + 2]
    const bits = (first << 16) | ((second ?? 0) << 8) | (third ?? 0)
    result += alphabet[(bits >> 18) & 63]
    result += alphabet[(bits >> 12) & 63]
    if (second !== undefined) result += alphabet[(bits >> 6) & 63]
    if (third !== undefined) result += alphabet[bits & 63]
  }
  return result
}

function confirmationId(category: string, values: readonly unknown[]): string {
  return `${category}:${base64Url(JSON.stringify([category, ...values]))}`
}

export function criterionConfirmationId(criterion: InferredCriterion): string {
  return confirmationId("criterion", [
    criterion.key,
    criterion.label,
    criterion.kind,
    criterion.evidence_required,
    criterion.source_text ?? null,
    criterion.recruiter_entered,
    criterion.lawful_requirement_confirmed,
  ])
}

export function adjacentConfirmationId(industryCode: string): string {
  return confirmationId("adjacent", [industryCode])
}

export function uncertaintyConfirmationId(
  uncertainty: string,
  position: number,
): string {
  return confirmationId("uncertainty", [position, uncertainty])
}

export function requiredInferenceIds(draft: InferenceDraft): string[] {
  return [
    ...draft.criteria
      .filter((criterion) => criterion.inferred)
      .map(criterionConfirmationId),
    ...draft.suggested_adjacent_industries.map(adjacentConfirmationId),
    ...draft.uncertainties.map(uncertaintyConfirmationId),
  ]
}
