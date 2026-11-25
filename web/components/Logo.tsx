/** The pager mark (a mint-tin pager in three-quarter view with a short antenna), inline. Same
 * drawing as `public/logo.svg`. The cream label and grey base keep it legible on the navy app bar
 * and on a white page. */

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
      <rect x="38" y="7" width="6.8" height="40.7" rx="3.4" fill="#14181f" />
      <circle cx="41.4" cy="10.4" r="1.6" fill="#fff4d6" />
      <path d="M7.7 74.8L8.2 72.7L9.6 70.7L26.9 51.1L29 49.3L31.6 47.8L34.7 46.7L38.2 46.1L41.7 46L45.3 46.3L48.6 47.1L111.8 67.2L114.8 68.5L117.2 70L119 71.9L120.1 74L120.3 76.1L120.3 92.2L119.8 94.3L118.4 96.3L101.1 115.9L99 117.7L96.4 119.1L93.3 120.2L89.8 120.8L86.3 121L82.7 120.7L79.4 119.9L16.2 99.7L13.2 98.5L10.8 96.9L9 95.1L7.9 93L7.7 90.8Z" fill="#93a1b1" />
      <path d="M7.7 66.2L8.2 64L9.6 62L26.9 42.4L29 40.6L31.6 39.2L34.7 38.1L38.2 37.5L41.7 37.3L45.3 37.6L48.6 38.4L111.8 58.6L114.8 59.8L117.2 61.4L119 63.3L120.1 65.3L120.3 67.5L120.3 76.1L119.8 78.2L118.4 80.3L101.1 99.9L99 101.6L96.4 103.1L93.3 104.2L89.8 104.8L86.3 105L82.7 104.6L79.4 103.8L16.2 83.7L13.2 82.5L10.8 80.9L9 79L7.9 76.9L7.7 74.8Z" fill="#062443" />
      <path d="M111.8 58.6L114.8 59.8L117.2 61.4L119 63.3L120.1 65.3L120.3 67.5L119.8 69.6L118.4 71.6L101.1 91.2L99 93L96.4 94.5L93.3 95.5L89.8 96.2L86.3 96.3L82.7 96L79.4 95.2L16.2 75L13.2 73.8L10.8 72.2L9 70.4L7.9 68.3L7.7 66.2L8.2 64L9.6 62L26.9 42.4L29 40.6L31.6 39.2L34.7 38.1L38.2 37.5L41.7 37.3L45.3 37.6L48.6 38.4Z" fill="#0b3c6f" />
      <path d="M103.9 62.8L103 64.3L103 65.9L103.8 67.5L105.3 68.8L107.5 69.8L92.4 86.8L89.9 86.3L87.2 86.2L84.6 86.7L82.4 87.6L80.7 88.9L24.1 70.9L25 69.3L25 67.7L24.2 66.2L22.7 64.8L20.5 63.8L35.6 46.9L38.1 47.4L40.8 47.4L43.4 46.9L45.6 46L47.3 44.7Z" fill="#fff4d6" />
      <path d="M81.8 62.5L84 63.4L85.6 64.8L86.3 66.3L86.3 67.9L85.4 69.5L81.9 73.4L80.3 74.7L78.1 75.6L75.5 76.1L72.8 76.1L70.3 75.5L48.5 68.6L46.3 67.6L44.8 66.3L44 64.7L44 63.1L44.9 61.6L48.4 57.6L50 56.3L52.2 55.4L54.8 54.9L57.5 55L60.1 55.5Z" fill="#0b3c6f" />
      <path d="M55 70.7L40.6 72.8L47.4 68.2Z" fill="#0b3c6f" />
      <circle cx="55.4" cy="62.4" r="2.6" fill="#fff4d6" />
      <circle cx="65.2" cy="65.5" r="2.6" fill="#fff4d6" />
      <circle cx="75" cy="68.6" r="2.6" fill="#fff4d6" />
    </svg>
  );
}
