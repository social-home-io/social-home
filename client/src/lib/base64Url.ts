/**
 * Base64URL (RFC 4648 §5) — URL-safe Base64 with `-`/`_` instead of
 * `+`/`/` and no `=` padding. Used to pack JSON payloads into
 * ``socialhome://…#…`` fragments and any other chat-safe single-line
 * carrier where ``+``/``/``/``=`` would get mangled by URL parsers,
 * email clients, or QR scanners.
 *
 * No DOM, no globals beyond the standard ``TextEncoder``/``btoa`` pair
 * that every browser ships — keeps the module trivially unit-testable.
 */

export function base64UrlEncode(text: string): string {
  const utf8 = new TextEncoder().encode(text)
  let bin = ''
  for (const byte of utf8) bin += String.fromCharCode(byte)
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function base64UrlDecode(text: string): string | null {
  const normalised = text.replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalised + '='.repeat((4 - (normalised.length % 4)) % 4)
  try {
    const bin = atob(padded)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return new TextDecoder().decode(bytes)
  } catch {
    return null
  }
}
