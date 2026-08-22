if (
  process.env.NODE_ENV === "production" &&
  process.env.ENABLE_DEV_AUTH_OVERRIDE === "true"
) {
  throw new Error("production_configuration_invalid")
}

await import("./server.js")
