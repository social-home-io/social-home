/**
 * uploadErrors — friendlier message for upload failures.
 *
 * Composer / DM / gallery / highlight / bazaar all surface raw exception
 * text on upload failures today (e.g. ``"Upload failed: TypeError:
 * Failed to fetch"``). That reads as "the app is broken" rather than
 * "your file is too big" or "we lost the network". Map the common
 * shapes to user-facing copy here so every caller can share the same
 * lookup.
 *
 * The classifier is heuristic: it inspects the error message + an
 * optional HTTP status (callers that throw ``new Error("Upload failed
 * (413)…")`` get richer matches). Falls back to a generic line if
 * nothing matches.
 */

export interface UploadErrorContext {
  /** Optional file the user picked — surfaces in size/type messages. */
  file?: File
}

export function describeUploadError(
  err: unknown,
  ctx: UploadErrorContext = {},
): string {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  const fileName = ctx.file?.name ?? ''
  const sizeMb = ctx.file ? Math.round(ctx.file.size / (1024 * 1024)) : null
  // Prefer name+size in the lead so the user knows *which* upload
  // failed when they're picking several files in a row.
  const lead = fileName ? `Couldn't upload ${fileName}` : 'Upload failed'

  // Network failures: TypeError on Failed to fetch / NetworkError /
  // explicit "Network error" string from the gallery's xhr path.
  if (/network|failed to fetch|err_network|networkerror/i.test(raw)) {
    return `${lead} — couldn't reach the server. Check your connection and try again.`
  }

  // Status-coded errors. The gallery xhr throws "Upload failed
  // (413): …"; api.ts throws "API 413: <path>".
  const statusMatch = raw.match(/\b(4\d\d|5\d\d)\b/)
  const status = statusMatch ? Number(statusMatch[1]) : null

  if (status === 401 || status === 403) {
    return `${lead} — you're not allowed to upload to this surface. Try signing in again.`
  }
  if (status === 413) {
    const detail = sizeMb != null ? ` (${sizeMb} MB)` : ''
    return `${lead}${detail} — file is too large for this server's limit.`
  }
  if (status === 415) {
    return `${lead} — that file type isn't supported. Try JPEG, PNG, WebP, or MP4.`
  }
  if (status === 429) {
    return `${lead} — you've uploaded a lot recently. Wait a minute and try again.`
  }
  if (status && status >= 500) {
    return `${lead} — the server hit an error. Try again in a moment.`
  }

  // Last-ditch: include the raw message but trim noise.
  const trimmed = raw.replace(/^Error:\s*/i, '').slice(0, 140)
  return trimmed ? `${lead} — ${trimmed}` : `${lead}.`
}
