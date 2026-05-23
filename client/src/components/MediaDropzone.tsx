/**
 * MediaDropzone — drag-and-drop + file-picker affordance for media
 * uploads. Matches the styling of the main post Composer's dropzone so
 * the highlight / momentum composers feel like one consistent surface.
 *
 * The component does NOT manage the upload itself — callers receive
 * the picked / dropped File[] via ``onFiles`` and decide what to do
 * with them (upload, validate, derive previews, etc.). That keeps
 * call-site flexibility for per-feature constraints (highlight's
 * per-day frame cap, momentum's 15-second video probe).
 */
import { useRef, useState } from 'preact/hooks'

interface MediaDropzoneProps {
  /** Called once per drop / pick with every selected File. */
  onFiles: (files: File[]) => void | Promise<void>
  /** Allow multi-file selection. Default: single file. */
  multiple?: boolean
  /** Native ``accept`` attribute for the input — e.g. ``image/*,video/*``. */
  accept?: string
  /** When true, the dropzone refuses interaction (no drag highlight,
   *  no clicks). Used by callers that have hit a per-day cap. */
  disabled?: boolean
  /** Resting hint, e.g. "Drag photos here, or". */
  hint?: string
  /** Action-link label, e.g. "choose photos…". */
  pickLabel?: string
  /** Hint shown while a drag is hovering, e.g. "Drop to attach". */
  draggingHint?: string
}

export function MediaDropzone({
  onFiles,
  multiple = false,
  accept,
  disabled = false,
  hint = 'Drag a file here, or',
  pickLabel = 'choose a file…',
  draggingHint = 'Drop to attach',
}: MediaDropzoneProps) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [dragActive, setDragActive] = useState(false)

  const handleFiles = async (files: File[]) => {
    if (disabled || files.length === 0) return
    await onFiles(files)
  }

  const onPicked = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    await handleFiles(files)
    input.value = ''
  }

  const onDrop = async (e: DragEvent) => {
    e.preventDefault()
    // Stop bubbling so a parent that also wires drop-anywhere handlers
    // (e.g. the post Composer's form-level drop) doesn't fire a second
    // time and double-process the same files.
    e.stopPropagation()
    setDragActive(false)
    if (disabled) return
    const dropped = Array.from(e.dataTransfer?.files ?? [])
    await handleFiles(dropped)
  }

  const onDragOver = (e: DragEvent) => {
    e.preventDefault()
    if (disabled) return
    if (!dragActive) setDragActive(true)
  }

  const onDragLeave = (e: DragEvent) => {
    if (e.currentTarget === e.target) setDragActive(false)
  }

  const cls = [
    'sh-mediadrop',
    dragActive ? 'sh-mediadrop--dragging' : '',
    disabled ? 'sh-mediadrop--disabled' : '',
  ].filter(Boolean).join(' ')

  return (
    <div
      class={cls}
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      <span>{dragActive ? draggingHint : hint}</span>
      <button
        type="button"
        class="sh-link"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
      >
        {pickLabel}
      </button>
      {/*
        The input MUST stay in the layout tree — the Android System
        WebView used by the HA Companion App does not fire the
        ``onChange`` callback when the input is ``display: none``
        (or ``hidden``). The user picks a file, the picker closes,
        ``input.files`` stays empty, and the composer silently shows
        nothing. ``.sr-only`` keeps the element 1×1 px offscreen so
        the picker callback fires correctly while staying invisible
        to the eye. Same fix applied at every other file-input site
        in the SPA — see git blame for the full list.
      */}
      <input
        ref={inputRef}
        type="file"
        multiple={multiple}
        accept={accept}
        disabled={disabled}
        class="sr-only"
        onChange={onPicked}
      />
    </div>
  )
}
