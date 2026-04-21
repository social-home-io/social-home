import type { JSX } from 'preact'

// Preact's generic ``HTMLAttributes<T>`` omits tag-specific attrs
// (``type``, ``disabled`` for <button>). Use the specialised
// ``ButtonHTMLAttributes`` so consumers can forward those props.
interface ButtonProps extends JSX.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger'
  loading?: boolean
}

export function Button({ variant = 'primary', loading, children, disabled, ...props }: ButtonProps) {
  return (
    <button
      class={`sh-btn sh-btn--${variant}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <span class="sh-spinner-sm" /> : children}
    </button>
  )
}
