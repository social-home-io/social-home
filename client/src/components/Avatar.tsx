import { withAuthToken } from '@/api'

interface AvatarProps {
  src?: string | null
  name: string
  size?: number
  onClick?: () => void
}

export function Avatar({ src, name, size = 40, onClick }: AvatarProps) {
  const initials = name.slice(0, 2).toUpperCase()
  const sizeStyle = { width: `${size}px`, height: `${size}px`, fontSize: `${size * 0.4}px` }
  if (src) {
    return (
      <img
        src={withAuthToken(src)}
        alt={name}
        class={`sh-avatar ${onClick ? 'sh-avatar--clickable' : ''}`}
        width={size}
        height={size}
        onClick={onClick}
      />
    )
  }
  return (
    <div
      class={`sh-avatar sh-avatar--initials ${onClick ? 'sh-avatar--clickable' : ''}`}
      style={sizeStyle}
      onClick={onClick}
    >
      {initials}
    </div>
  )
}
