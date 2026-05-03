/**
 * VideoPlayer — custom video player (§23.108).
 */
import { useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'

export function VideoPlayer({ src, poster }: { src: string; poster?: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  // ``useSignal`` so the play / pause writes land in the same instance
  // the JSX's ``playing.value`` reads from on every render.
  const playing = useSignal(false)

  const togglePlay = () => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) { v.play(); playing.value = true }
    else { v.pause(); playing.value = false }
  }

  return (
    <div class="sh-video-player" onClick={togglePlay}>
      <video ref={videoRef} src={src} poster={poster} preload="metadata" playsinline
        class="sh-video-element"
        onEnded={() => playing.value = false} />
      {!playing.value && (
        <div class="sh-video-overlay">
          <button class="sh-video-play" aria-label="Play">▶</button>
        </div>
      )}
    </div>
  )
}
