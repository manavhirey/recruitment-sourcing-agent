import { requirePageContext } from "@/lib/page-context"

export const metadata = { title: "Settings" }

export default async function SettingsPage() {
  const context = await requirePageContext()
  return (
    <div className="page-stack">
      <header className="page-header"><div><p className="eyebrow">Agency controls</p><h1>Settings</h1></div></header>
      <section className="settings-card"><h2>Workspace access</h2><p>Your current role is <strong>{context.me.role}</strong>.</p><p>Provider credentials and operator controls are never exposed in agency settings.</p></section>
    </div>
  )
}
