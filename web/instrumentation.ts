import { assertProductionEnvironment } from "@/production-env"

export async function register(): Promise<void> {
  if (process.env.NODE_ENV === "production") {
    assertProductionEnvironment(process.env)
  }
}
