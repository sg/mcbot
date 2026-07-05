import { fmtDateTime } from '../../time.js'

export function cols(rows) {
  return rows && rows.length ? Object.keys(rows[0]).filter((c) => !c.startsWith('_')) : []
}

export function cell(v) {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// generic-table cell, but render epoch 'ts' columns (the audit log) as ISO
export function cellAt(row, c) {
  if (c === 'ts' && row[c]) return fmtDateTime(row[c])
  return cell(row[c])
}
