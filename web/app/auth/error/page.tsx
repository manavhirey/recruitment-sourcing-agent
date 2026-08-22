import Link from "next/link"

export const metadata = { title: "Sign-in error" }

export default function AuthErrorPage() {
  return (
    <main className="auth-page">
      <section className="auth-card" role="alert">
        <p className="eyebrow">Access stopped</p>
        <h1>We could not complete sign in.</h1>
        <p>No account or provider details were disclosed. Start a new sign-in attempt.</p>
        <Link className="button button-primary" href="/api/auth/signin">Try sign in again</Link>
      </section>
    </main>
  )
}
