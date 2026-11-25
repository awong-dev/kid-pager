/** The pager mark (a mint-tin pager with an antenna), inline. Same drawing as `public/logo.svg`.
 * The cream label and antenna keep it legible on both the blue app bar and a white page. */

interface LogoProps {
  size?: number;
  /** Accessible name; omit for a decorative mark (hidden from assistive tech). */
  title?: string;
}

export default function Logo({ size = 32, title }: LogoProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 128 128"
      width={size}
      height={size}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
      style={{ display: "block", flexShrink: 0 }}
    >
      {title && <title>{title}</title>}
      <rect x="32" y="64" width="88" height="48" rx="11" fill="#c9d3dc" />
      <rect x="28" y="34" width="96" height="68" rx="16" fill="#2f5d8a" />
      <path
        d="M45 44H107A7 7 0 0 0 114 51V85A7 7 0 0 0 107 92H45A7 7 0 0 0 38 85V51A7 7 0 0 0 45 44Z"
        fill="#fbf1d8"
      />
      <path
        d="M68 55H84A8 8 0 0 1 92 63V68A8 8 0 0 1 84 76H77L66 85L68 76A8 8 0 0 1 60 68V63A8 8 0 0 1 68 55Z"
        fill="#2f5d8a"
      />
      <circle cx="69" cy="65.5" r="2.3" fill="#fbf1d8" />
      <circle cx="76" cy="65.5" r="2.3" fill="#fbf1d8" />
      <circle cx="83" cy="65.5" r="2.3" fill="#fbf1d8" />
      <rect x="9" y="14" width="13" height="72" rx="6.5" fill="#1b1f27" />
      <rect x="9" y="84" width="25" height="13" rx="4.5" fill="#1b1f27" />
    </svg>
  );
}
