import { CandidateDirectory } from "@/components/candidates/CandidateDirectory"
import { apiFetch } from "@/lib/api"
import { requirePageContext } from "@/lib/page-context"
import type { CandidateDirectoryPage } from "@/lib/schemas"

export const metadata = { title: "Candidates" }

export default async function CandidatesPage() {
  const context = await requirePageContext()
  const initialPage = await apiFetch<CandidateDirectoryPage>(
    "/api/v1/candidates?limit=50",
    context.tenantId,
  )
  return (
    <div className="page-stack">
      <header className="page-header"><div><p className="eyebrow">Talent directory</p><h1>Candidates</h1><p>Search canonical people and open only the job history authorized for this agency view.</p></div></header>
      <CandidateDirectory initialPage={initialPage} />
    </div>
  )
}
