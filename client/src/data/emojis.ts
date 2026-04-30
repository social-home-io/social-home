/**
 * Shared emoji table — single source of truth for the reaction picker
 * AND the Slack-style ``:shortcode:`` autocomplete in text inputs.
 *
 * Each entry pairs an emoji with a space-separated keyword string.
 * Keep keywords lowercase; the autocomplete + picker search both
 * substring-match against ``keywords`` so any keyword (or the literal
 * shortcode-name as the first keyword) can trigger an emoji.
 *
 * The first keyword on each entry doubles as the canonical
 * ``:shortcode:`` token — the autocomplete prefers it on a tied
 * ranking, and the ``:foo:`` literal-replace path uses it for
 * "user types ``:smile:`` and we swap in 😄" without an explicit
 * dropdown selection.
 */

export interface EmojiEntry {
  emoji: string
  keywords: string
}

export const FREQUENT_EMOJI: readonly string[] = [
  '👍', '❤️', '😂', '😮', '😢', '🎉', '🔥', '👏',
]

export const ALL_EMOJI_WITH_KEYWORDS: readonly EmojiEntry[] = [
  { emoji: '👍', keywords: 'thumbsup thumbs up yes ok like approve +1' },
  { emoji: '👎', keywords: 'thumbsdown thumbs down no dislike reject -1' },
  { emoji: '❤️', keywords: 'heart love red' },
  { emoji: '🔥', keywords: 'fire hot lit flame' },
  { emoji: '😂', keywords: 'joy lol laugh cry funny' },
  { emoji: '😮', keywords: 'wow surprised shocked open mouth' },
  { emoji: '😢', keywords: 'cry sad tear' },
  { emoji: '😡', keywords: 'angry mad rage' },
  { emoji: '🎉', keywords: 'tada party celebrate congrats' },
  { emoji: '👏', keywords: 'clap applause bravo' },
  { emoji: '🤔', keywords: 'thinking hmm' },
  { emoji: '👀', keywords: 'eyes looking see' },
  { emoji: '💯', keywords: 'hundred 100 perfect' },
  { emoji: '✅', keywords: 'check done tick yes' },
  { emoji: '❌', keywords: 'cross x no wrong' },
  { emoji: '⭐', keywords: 'star favourite favorite' },
  { emoji: '🙏', keywords: 'pray thanks please' },
  { emoji: '💪', keywords: 'muscle strong flex' },
  { emoji: '🫡', keywords: 'salute respect' },
  { emoji: '🤝', keywords: 'handshake deal agree' },
  { emoji: '😊', keywords: 'smile happy blush' },
  { emoji: '😎', keywords: 'cool sunglasses' },
  { emoji: '🥳', keywords: 'partying party celebrate birthday' },
  { emoji: '😴', keywords: 'sleep sleeping tired zzz' },
  { emoji: '🤯', keywords: 'mindblown exploding head' },
  { emoji: '💀', keywords: 'skull dead rip' },
  { emoji: '👻', keywords: 'ghost spooky halloween' },
  { emoji: '🎯', keywords: 'target bullseye focus' },
  { emoji: '🚀', keywords: 'rocket launch fast ship' },
  { emoji: '💡', keywords: 'idea bulb light' },
  { emoji: '🏠', keywords: 'home house' },
  { emoji: '🍕', keywords: 'pizza food' },
  { emoji: '☕', keywords: 'coffee tea drink' },
  { emoji: '🎵', keywords: 'music note song' },
  { emoji: '📚', keywords: 'books read study' },
  { emoji: '🔧', keywords: 'wrench tool fix' },
  { emoji: '🌟', keywords: 'sparkle star glowing' },
  { emoji: '💎', keywords: 'gem diamond jewel' },
  { emoji: '🌈', keywords: 'rainbow colorful pride' },
  { emoji: '🦋', keywords: 'butterfly insect' },
]

export const ALL_EMOJI: readonly string[] = ALL_EMOJI_WITH_KEYWORDS.map(e => e.emoji)

/** Substring match against the keyword string. Simple, correct, fast. */
export function emojiMatches(entry: EmojiEntry, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return entry.keywords.includes(q)
}

/** Look up an exact ``:shortcode:`` (e.g. ``smile`` → 😊). The first
 *  keyword on each entry is treated as the canonical shortcode for
 *  literal-replace; later keywords still match via the autocomplete. */
export function emojiByShortcode(shortcode: string): string | null {
  const q = shortcode.trim().toLowerCase()
  if (!q) return null
  for (const entry of ALL_EMOJI_WITH_KEYWORDS) {
    const first = entry.keywords.split(' ')[0]
    if (first === q) return entry.emoji
  }
  return null
}

/** Rank emoji entries against a partial shortcode query for the
 *  autocomplete dropdown. Prefers entries whose first keyword starts
 *  with the query (canonical match), then any keyword that starts
 *  with it, then any substring match. Caps at ``limit`` results. */
export function searchEmoji(query: string, limit = 8): EmojiEntry[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const exact: EmojiEntry[] = []
  const startsWith: EmojiEntry[] = []
  const substring: EmojiEntry[] = []
  for (const entry of ALL_EMOJI_WITH_KEYWORDS) {
    const words = entry.keywords.split(' ')
    if (words[0] === q) {
      exact.push(entry)
    } else if (words.some(w => w.startsWith(q))) {
      startsWith.push(entry)
    } else if (entry.keywords.includes(q)) {
      substring.push(entry)
    }
  }
  return [...exact, ...startsWith, ...substring].slice(0, limit)
}
