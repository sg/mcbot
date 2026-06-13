// Shared timestamp formatting. Inputs are Unix epoch seconds; output is local
// time. Two formats only: 24-hour clock time for compact list views, and an
// ISO-style date-time for detail/inspector views.

function pad(n) {
  return String(n).padStart(2, '0')
}

// 24-hour clock, HH:MM:SS (no AM/PM).
export function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// ISO-style local date-time, YYYY-MM-DD HH:MM:SS.
export function fmtDateTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}
