/**
 * Product mark: the app icon, drawn inline so it inherits the current size.
 * Keep it identical to `public/icon.svg` and the iOS `AppIcon`.
 */
export function Mark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      aria-hidden
      focusable="false"
      style={{ display: 'inline-block', flex: 'none' }}
    >
      <rect width="64" height="64" rx="14" fill="#161616" />
      <g stroke="#9A9A9A" strokeWidth="2.1">
        <path d="M24.3 22.7H39.7" />
        <path d="M21.74 27.35 29.06 38.95" />
        <path d="M42.26 27.35 34.94 38.95" />
      </g>
      <g fill="#FFFFFF">
        <circle cx="18.8" cy="22.7" r="3.9" />
        <circle cx="45.2" cy="22.7" r="3.9" />
        <circle cx="32" cy="43.6" r="3.9" />
      </g>
    </svg>
  );
}
