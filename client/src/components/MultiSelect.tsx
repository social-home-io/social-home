/**
 * MultiSelect — bulk selection mode for lists (§23.34).
 */
import { signal } from '@preact/signals'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

export const multiSelectMode = signal(false)
export const selectedIds = signal<Set<string>>(new Set())

export function toggleMultiSelect() {
  multiSelectMode.value = !multiSelectMode.value
  if (!multiSelectMode.value) selectedIds.value = new Set()
}

export function toggleItem(id: string) {
  const s = new Set(selectedIds.value)
  if (s.has(id)) s.delete(id); else s.add(id)
  selectedIds.value = s
}

export function MultiSelectBar({ onAction }: { onAction: (ids: string[], action: string) => void }) {
  if (!multiSelectMode.value || selectedIds.value.size === 0) return null
  const ids = Array.from(selectedIds.value)
  return (
    <div class="sh-multi-bar" role="toolbar">
      <span>{ids.length} selected</span>
      <Button variant="secondary" onClick={() => onAction(ids, 'delete')}>{t('common.delete')}</Button>
      <Button variant="secondary" onClick={() => onAction(ids, 'mark_read')}>Mark read</Button>
      <Button variant="secondary" onClick={() => { selectedIds.value = new Set(); multiSelectMode.value = false }}>{t('common.cancel')}</Button>
    </div>
  )
}

export function SelectCheckbox({ id }: { id: string }) {
  if (!multiSelectMode.value) return null
  return (
    <input type="checkbox" class="sh-select-cb"
      checked={selectedIds.value.has(id)}
      onChange={() => toggleItem(id)}
      aria-label={`Select item ${id}`} />
  )
}
