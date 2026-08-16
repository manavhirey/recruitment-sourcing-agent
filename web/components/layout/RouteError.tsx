"use client"

export function RouteError({ reset }: { reset: () => void }) {
  return (
    <div className="route-state" role="alert">
      <p className="eyebrow">Something went wrong</p>
      <h1>This workspace could not be loaded.</h1>
      <p>No sensitive error details were displayed. Retry or return to the jobs page.</p>
      <div className="state-actions">
        <button className="button button-primary" type="button" onClick={reset}>Try again</button>
        <a className="button button-secondary" href="/jobs">Back to jobs</a>
      </div>
    </div>
  )
}
