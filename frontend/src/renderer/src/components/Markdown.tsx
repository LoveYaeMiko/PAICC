import type { ReactNode } from 'react'

/**
 * A tiny, dependency-free Markdown renderer tailored to LLM output.
 *
 * Supports the constructs an assistant most often emits: fenced code blocks, inline
 * code, headings, bold/italic, links, ordered/unordered lists, blockquotes, horizontal
 * rules, tables and paragraphs. It intentionally avoids the full CommonMark surface
 * (no raw HTML, no nested lists) in exchange for zero build-time dependencies.
 */

// Inline: code span, bold, italic, link.
const INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]*\]\([^)\s]*\))/g

function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  INLINE_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const full = m[0]
    if (m[1]) {
      out.push(
        <code key={key++} className="md-inline-code">
          {m[1].slice(1, -1)}
        </code>,
      )
    } else if (m[2]) {
      out.push(<strong key={key++}>{m[2].slice(2, -2)}</strong>)
    } else if (m[3]) {
      out.push(<em key={key++}>{m[3].slice(1, -1)}</em>)
    } else if (m[4]) {
      const mm = /^\[([^\]]*)\]\(([^)]*)\)$/.exec(m[4])
      out.push(
        <a key={key++} href={mm?.[2] ?? '#'} target="_blank" rel="noreferrer">
          {mm?.[1] ?? m[4]}
        </a>,
      )
    }
    last = m.index + full.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const HEADING_RE = /^(#{1,6})\s+(.*)$/
const HR_RE = /^\s*([-*_])(\s*\1\s*){2,}$/
const UL_RE = /^\s*[-*+]\s+/
const OL_RE = /^\s*\d+[.)]\s+/

function isTableSeparator(line: string): boolean {
  return /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*$/.test(line) && line.includes('-')
}

function splitTableRow(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|')) s = s.slice(0, -1)
  return s.split('|').map((c) => c.trim())
}

export default function Markdown({ text }: { text: string }): JSX.Element {
  const lines = (text ?? '').replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]
    const trimmed = line.trim()

    // Fenced code block.
    if (trimmed.startsWith('```')) {
      const lang = trimmed.slice(3).trim()
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        code.push(lines[i])
        i++
      }
      i++ // closing fence
      blocks.push(
        <pre key={key++} className="md-pre">
          <code className={lang ? `md-code-block lang-${lang}` : 'md-code-block'}>{code.join('\n')}</code>
        </pre>,
      )
      continue
    }

    if (!trimmed) {
      i++
      continue
    }

    // Heading.
    const hm = HEADING_RE.exec(trimmed)
    if (hm) {
      const level = hm[1].length
      blocks.push(
        <div key={key++} className={`md-h md-h${level}`}>
          {renderInline(hm[2])}
        </div>,
      )
      i++
      continue
    }

    // Horizontal rule.
    if (HR_RE.test(trimmed)) {
      blocks.push(<hr key={key++} className="md-hr" />)
      i++
      continue
    }

    // Blockquote.
    if (/^\s*>\s?/.test(trimmed)) {
      const quote: string[] = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i].trim())) {
        quote.push(lines[i].trim().replace(/^\s*>\s?/, ''))
        i++
      }
      blocks.push(
        <blockquote key={key++} className="md-quote">
          {renderInline(quote.join(' '))}
        </blockquote>,
      )
      continue
    }

    // Unordered list.
    if (UL_RE.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && UL_RE.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(UL_RE, ''))
        i++
      }
      blocks.push(
        <ul key={key++} className="md-list">
          {items.map((it, n) => (
            <li key={n}>{renderInline(it)}</li>
          ))}
        </ul>,
      )
      continue
    }

    // Ordered list.
    if (OL_RE.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && OL_RE.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(OL_RE, ''))
        i++
      }
      blocks.push(
        <ol key={key++} className="md-list">
          {items.map((it, n) => (
            <li key={n}>{renderInline(it)}</li>
          ))}
        </ol>,
      )
      continue
    }

    // Table: header row + separator row.
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1].trim())) {
      const header = splitTableRow(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].trim().includes('|')) {
        rows.push(splitTableRow(lines[i]))
        i++
      }
      blocks.push(
        <table key={key++} className="md-table">
          <thead>
            <tr>
              {header.map((h, n) => (
                <th key={n}>{renderInline(h)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {r.map((c, ci) => (
                  <td key={ci}>{renderInline(c)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      )
      continue
    }

    // Paragraph: gather until a blank line or a block-starting line.
    const para: string[] = [trimmed]
    i++
    while (i < lines.length) {
      const t = lines[i].trim()
      if (
        !t ||
        t.startsWith('```') ||
        HEADING_RE.test(t) ||
        HR_RE.test(t) ||
        /^\s*>\s?/.test(t) ||
        UL_RE.test(t) ||
        OL_RE.test(t) ||
        (t.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1].trim()))
      ) {
        break
      }
      para.push(t)
      i++
    }
    blocks.push(
      <p key={key++} className="md-p">
        {renderInline(para.join(' '))}
      </p>,
    )
  }

  return <div className="md-root">{blocks}</div>
}
