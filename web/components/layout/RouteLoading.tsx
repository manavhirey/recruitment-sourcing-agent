export function RouteLoading() {
  return (
    <div className="route-state" role="status" aria-live="polite">
      <span className="loading-mark" aria-hidden="true" />
      <h1>Loading workspace</h1>
      <p>Checking your agency access and current work.</p>
    </div>
  )
}
