import { spawnSync } from "node:child_process"
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import openapiTS, { astToString } from "openapi-typescript"

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const repositoryRoot = resolve(webRoot, "..")
const checkOnly = process.argv.includes("--check")
const temporaryDirectory = mkdtempSync(join(tmpdir(), "sourcing-openapi-"))

function pythonCandidates() {
  const configured = process.env.BACKEND_PYTHON
  return [
    configured,
    join(repositoryRoot, "backend", ".venv", "bin", "python"),
    "python3.12",
    "python3",
  ].filter(Boolean)
}

function exportOpenApi(destination) {
  const script = join(webRoot, "scripts", "export-openapi.py")
  const failures = []
  for (const python of pythonCandidates()) {
    if (python.includes("/") && !existsSync(python)) continue
    const result = spawnSync(python, [script, destination], {
      cwd: repositoryRoot,
      encoding: "utf8",
      env: process.env,
    })
    if (result.status === 0) return
    failures.push(`${python}: ${(result.stderr || result.stdout).trim()}`)
  }
  throw new Error(
    `Unable to export backend OpenAPI. Install backend locked dependencies first.\n${failures.join("\n")}`,
  )
}

function taxonomySource() {
  const taxonomy = JSON.parse(
    readFileSync(
      join(repositoryRoot, "backend", "app", "clients", "industry_taxonomy.v1.json"),
      "utf8",
    ),
  )
  return `/* Generated from backend/app/clients/industry_taxonomy.v1.json. Do not edit. */\nexport const industryTaxonomy = ${JSON.stringify(taxonomy, null, 2)} as const\n`
}

function emit(path, content) {
  if (checkOnly) {
    if (!existsSync(path) || readFileSync(path, "utf8") !== content) {
      throw new Error(`${path.slice(webRoot.length + 1)} is out of date; run npm run api:generate`)
    }
    return
  }
  writeFileSync(path, content, "utf8")
}

try {
  const openApiPath = join(temporaryDirectory, "openapi.json")
  exportOpenApi(openApiPath)
  const schema = JSON.parse(readFileSync(openApiPath, "utf8"))
  const nodes = await openapiTS(schema, { alphabetize: true, immutable: true })
  emit(
    join(webRoot, "lib", "generated-api.ts"),
    `/* Generated from the backend OpenAPI document. Do not edit. */\n${astToString(nodes)}`,
  )
  emit(join(webRoot, "lib", "generated-taxonomy.ts"), taxonomySource())
  process.stdout.write(checkOnly ? "Generated API artifacts are current.\n" : "Generated API artifacts updated.\n")
} finally {
  rmSync(temporaryDirectory, { recursive: true, force: true })
}
