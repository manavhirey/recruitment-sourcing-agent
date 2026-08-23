import "server-only"

import Busboy, { type BusboyFileStream } from "@fastify/busboy"
import { once } from "node:events"

const maximumFileBytes = 10_000_000
const maximumBodyBytes = maximumFileBytes + 16_384
const maximumContentTypeBytes = 1_024
const maximumHeaderBytes = 8_192
const maximumHeaderPairs = 8

export type MultipartUpload = {
  contents: Uint8Array
  filename: string
  mediaType: string
}

export class MultipartUploadError extends Error {
  constructor(
    public readonly code:
      | "job_description_file_required"
      | "job_description_file_too_large",
  ) {
    super(code)
  }
}

function declaredBodyTooLarge(request: Request): boolean {
  const value = request.headers.get("Content-Length")
  if (value === null) return false
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > maximumBodyBytes
}

export async function readMultipartUpload(
  request: Request,
): Promise<MultipartUpload> {
  const contentType = request.headers.get("Content-Type") ?? ""
  if (
    !contentType.toLowerCase().startsWith("multipart/form-data") ||
    Buffer.byteLength(contentType) > maximumContentTypeBytes
  ) {
    throw new MultipartUploadError("job_description_file_required")
  }
  if (declaredBodyTooLarge(request)) {
    throw new MultipartUploadError("job_description_file_too_large")
  }
  if (!request.body) {
    throw new MultipartUploadError("job_description_file_required")
  }

  let parser
  try {
    parser = Busboy({
      headers: { "content-type": contentType },
      limits: {
        fields: 0,
        files: 1,
        parts: 1,
        fileSize: maximumFileBytes,
        headerPairs: maximumHeaderPairs,
        headerSize: maximumHeaderBytes,
      },
    })
  } catch {
    throw new MultipartUploadError("job_description_file_required")
  }

  let failure: MultipartUploadError | null = null
  let fileCount = 0
  let filename = ""
  let mediaType = ""
  const fileChunks: Buffer[] = []
  const fail = (error: MultipartUploadError) => {
    failure ??= error
  }
  const rejectShape = () => {
    fail(new MultipartUploadError("job_description_file_required"))
  }

  parser.on(
    "file",
    (
      fieldName: string,
      stream: BusboyFileStream,
      partFilename: string,
      _transferEncoding: string,
      partMediaType: string,
    ) => {
      fileCount += 1
      if (fieldName !== "file" || fileCount !== 1) rejectShape()
      filename = partFilename
      mediaType = partMediaType
      stream.on("limit", () => {
        fail(
          new MultipartUploadError("job_description_file_too_large"),
        )
      })
      stream.on("data", (chunk: Buffer) => {
        if (failure === null) fileChunks.push(Buffer.from(chunk))
      })
      stream.on("error", rejectShape)
    },
  )
  parser.on("field", rejectShape)
  parser.on("partsLimit", rejectShape)
  parser.on("filesLimit", rejectShape)
  parser.on("fieldsLimit", rejectShape)

  const parserFinished = new Promise<void>((resolve, reject) => {
    parser.once("finish", resolve)
    parser.once("error", () => {
      reject(new MultipartUploadError("job_description_file_required"))
    })
  })
  const reader = request.body.getReader()
  let totalBytes = 0
  let headerProbe = Buffer.alloc(0)
  let headersComplete = false
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      totalBytes += value.byteLength
      if (totalBytes > maximumBodyBytes) {
        throw new MultipartUploadError("job_description_file_too_large")
      }
      if (!headersComplete) {
        const maximumProbeBytes =
          maximumHeaderBytes + maximumContentTypeBytes + 4
        const remaining = maximumProbeBytes + 1 - headerProbe.byteLength
        headerProbe = Buffer.concat([
          headerProbe,
          Buffer.from(value).subarray(0, Math.max(0, remaining)),
        ])
        const headerEnd = headerProbe.indexOf("\r\n\r\n")
        if (headerEnd >= 0) {
          const boundaryEnd = headerProbe.indexOf("\r\n")
          const headerCount = boundaryEnd < 0
            ? 0
            : headerProbe
              .subarray(boundaryEnd + 2, headerEnd)
              .toString("latin1")
              .split("\r\n")
              .filter(Boolean).length
          if (
            boundaryEnd < 2 ||
            headerEnd - boundaryEnd - 2 > maximumHeaderBytes ||
            headerCount > maximumHeaderPairs
          ) {
            throw new MultipartUploadError(
              "job_description_file_required",
            )
          }
          headersComplete = true
        } else if (headerProbe.byteLength > maximumProbeBytes) {
          throw new MultipartUploadError("job_description_file_required")
        }
      }
      if (!parser.write(Buffer.from(value))) {
        await once(parser, "drain")
      }
      if (failure) throw failure
    }
    parser.end()
    await parserFinished
    if (failure) throw failure
  } finally {
    reader.releaseLock()
    if (!parser.destroyed) parser.destroy()
  }

  if (fileCount !== 1 || !filename) {
    throw new MultipartUploadError("job_description_file_required")
  }
  return {
    contents: new Uint8Array(Buffer.concat(fileChunks)),
    filename,
    mediaType,
  }
}
