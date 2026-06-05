/**
 * Shared bazaar currency formatting.
 *
 * Lives in its own module so ``BazaarOffersPanel`` (and other bazaar
 * surfaces) can use ``formatBazaarAmount`` without importing it back from
 * ``BazaarPostBody`` — which imports ``BazaarOffersPanel`` as a child and
 * therefore formed a ``BazaarPostBody ↔ BazaarOffersPanel`` import cycle.
 */

/** Currencies stored as whole units (no minor unit / cents). All others are
 *  stored as integer minor units (cents) and divided by 100 for display. */
export const CURRENCY_FRACTION_DIGITS: Record<string, number> = {
  JPY: 0, KRW: 0, ISK: 0,
}

export function formatBazaarAmount(
  amount: number | null | undefined, currency: string,
): string {
  if (amount == null) return '—'
  const digits = CURRENCY_FRACTION_DIGITS[currency] ?? 2
  const value = digits === 0 ? amount : amount / 100
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency,
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(value)
}
