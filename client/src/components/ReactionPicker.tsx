/**
 * ReactionPicker — emoji reaction selection (§23.45).
 */
import { signal } from '@preact/signals'
import {
  ALL_EMOJI,
  ALL_EMOJI_WITH_KEYWORDS,
  FREQUENT_EMOJI,
  emojiMatches,
} from '@/data/emojis'

interface ReactionPickerProps {
  onSelect: (emoji: string) => void
  onClose: () => void
}

const search = signal('')

export function ReactionPicker({ onSelect, onClose }: ReactionPickerProps) {
  const filtered = search.value
    ? ALL_EMOJI_WITH_KEYWORDS.filter(e => emojiMatches(e, search.value)).map(e => e.emoji)
    : ALL_EMOJI

  return (
    <div class="sh-reaction-picker" onClick={(e) => e.stopPropagation()}>
      <div class="sh-reaction-picker-header">
        <input class="sh-reaction-search" placeholder="Search emoji..."
          value={search.value}
          onInput={(e) => search.value = (e.target as HTMLInputElement).value} />
        <button class="sh-reaction-close" onClick={onClose}>✕</button>
      </div>
      <div class="sh-reaction-frequent">
        {FREQUENT_EMOJI.map(e => (
          <button key={e} class="sh-emoji-btn" onClick={() => { onSelect(e); onClose() }}>
            {e}
          </button>
        ))}
      </div>
      <div class="sh-reaction-grid">
        {filtered.map(e => (
          <button key={e} class="sh-emoji-btn" onClick={() => { onSelect(e); onClose() }}>
            {e}
          </button>
        ))}
      </div>
    </div>
  )
}
