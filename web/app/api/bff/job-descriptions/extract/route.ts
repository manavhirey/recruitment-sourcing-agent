import { handleDocumentExtraction } from "@/lib/document-extraction-bff"

export async function POST(request: Request): Promise<Response> {
  const appUrl = process.env.AUTH_URL
  if (!appUrl) {
    return Response.json(
      { code: "authentication_configuration_invalid" },
      {
        status: 503,
        headers: { "Cache-Control": "private, no-store" },
      },
    )
  }
  return handleDocumentExtraction(request, { appUrl })
}
