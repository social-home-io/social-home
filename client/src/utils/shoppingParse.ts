/**
 * Parse a single shopping-list segment into a ``{text, store}`` pair.
 *
 * The household shopping page accepts a power-user shortcut on the
 * quick-add input: ``"Milk @ Aldi"`` becomes ``text="Milk",
 * store="Aldi"``. Comma-splitting happens at the call site (each
 * segment runs through this function independently), so a batch like
 * ``"Milk @ Aldi, Bread @ Bakery, Eggs"`` ends up with three items —
 * the last one no store.
 *
 * Rules:
 *
 *  - Split on the **last** ``@`` so a store name with an ``@`` in it
 *    (rare, but think "Wine @ Whole Foods (4th @ Main)") still works
 *    by quoting habit — the last ``@`` wins.
 *  - Surrounding whitespace on either side of ``@`` is trimmed.
 *  - An empty text or empty store after the trim collapses that side
 *    to "no value" rather than producing whitespace-only fields:
 *    ``"@ Aldi"`` → ``text="", store="Aldi"`` (caller decides
 *    whether to skip empty text); ``"Milk @"`` →
 *    ``text="Milk", store=null``.
 */
export interface ParsedShoppingItem {
  text: string
  store: string | null
}

export function parseItemInput(raw: string): ParsedShoppingItem {
  const trimmed = (raw ?? '').trim()
  if (!trimmed) return { text: '', store: null }

  const lastAt = trimmed.lastIndexOf('@')
  if (lastAt < 0) return { text: trimmed, store: null }

  const text = trimmed.slice(0, lastAt).trim()
  const store = trimmed.slice(lastAt + 1).trim()
  return {
    text,
    store: store ? store : null,
  }
}
