/**
 * Spinner - SVG-based loading spinner with native SMIL animation.
 * Uses <animateTransform> which is immune to CSS resets (Tailwind, appkit-ui, etc.)
 */

interface SpinnerProps {
  size?: number;
  trackColor?: string;
  spinColor?: string;
  strokeWidth?: number;
}

export function Spinner({
  size = 16,
  trackColor = "#e2e8f0",
  spinColor = "#2563eb",
  strokeWidth = 3,
}: SpinnerProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={{ display: "inline-block", verticalAlign: "middle" }}
    >
      {/* Background track (full circle, light gray) */}
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke={trackColor}
        strokeWidth={strokeWidth}
      />
      {/* Spinning arc (partial circle, colored) */}
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke={spinColor}
        strokeWidth={strokeWidth}
        strokeDasharray="20 43"
        strokeLinecap="round"
      >
        <animateTransform
          attributeName="transform"
          type="rotate"
          from="0 12 12"
          to="360 12 12"
          dur="0.8s"
          repeatCount="indefinite"
        />
      </circle>
    </svg>
  );
}

/** White spinner variant for dark backgrounds (e.g. send button) */
export function SpinnerWhite({ size = 16 }: { size?: number }) {
  return (
    <Spinner
      size={size}
      trackColor="rgba(255,255,255,0.3)"
      spinColor="white"
    />
  );
}
