import './ProgressBar.css'

/**
 * Shared by both update flows that stream a download (zapret's own strategy
 * update and BridgeBox's self-update) - one place decides what "downloading"
 * looks like, so the two cannot drift into different bars.
 *
 * `percent` is null for a phase with no byte count to show (verifying,
 * extracting) - renders an indeterminate sliding stripe instead of a stalled
 * bar sitting at whatever percent the download phase left it at.
 */
export function ProgressBar({ percent }: { percent: number | null }) {
  const known = percent !== null
  return (
    <div
      className={`bb-progress${known ? '' : ' bb-progress--indeterminate'}`}
      role="progressbar"
      aria-valuenow={known ? Math.round(percent) : undefined}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="bb-progress__fill"
        style={known ? { width: `${Math.min(100, Math.max(0, percent))}%` } : undefined}
      />
    </div>
  )
}
