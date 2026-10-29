/** Text wordmark with a simple geometric mark — no image asset needed. */
export function Wordmark() {
  return (
    <span className="flex items-center gap-2.5">
      <svg viewBox="0 0 28 28" className="h-7 w-7" aria-hidden="true">
        <rect
          x="0.5"
          y="0.5"
          width="27"
          height="27"
          rx="7"
          fill="#0b1324"
          stroke="rgba(103,232,249,0.35)"
        />
        <path
          d="M7 15.5h3.2l2-5 3.4 9 2.2-5.5H21"
          fill="none"
          stroke="#67e8f9"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-[15px] font-semibold tracking-[0.14em] text-white">ESSR</span>
    </span>
  );
}
