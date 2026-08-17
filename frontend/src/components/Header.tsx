/**
 * App Header - with Narrative Deck download + Template Refresh + README
 */

import { useState } from "react";
import { Spinner } from "./Spinner";
import { downloadNarrativeDeck, refreshTemplates, fetchReadme } from "../api";

export function Header() {
  const [deckLoading, setDeckLoading] = useState(false);
  const [deckError, setDeckError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState("");
  const [readmeOpen, setReadmeOpen] = useState(false);
  const [readmeContent, setReadmeContent] = useState("");
  const [readmeLoading, setReadmeLoading] = useState(false);

  const handleDeckDownload = async () => {
    setDeckLoading(true);
    setDeckError("");
    try {
      await downloadNarrativeDeck();
    } catch (err: unknown) {
      setDeckError(err instanceof Error ? err.message : "Download failed");
      setTimeout(() => setDeckError(""), 4000);
    } finally {
      setDeckLoading(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    setRefreshMsg("");
    try {
      const res = await refreshTemplates();
      if (res.success) {
        setRefreshMsg(`${res.active_templates} active templates loaded`);
      } else {
        setRefreshMsg(res.error || "Refresh failed");
      }
      setTimeout(() => setRefreshMsg(""), 4000);
    } catch {
      setRefreshMsg("Refresh failed");
      setTimeout(() => setRefreshMsg(""), 4000);
    } finally {
      setRefreshing(false);
    }
  };

  const handleReadme = async () => {
    setReadmeOpen(true);
    setReadmeLoading(true);
    try {
      const content = await fetchReadme();
      setReadmeContent(content);
    } catch {
      setReadmeContent("# Error\n\nCould not load README.");
    } finally {
      setReadmeLoading(false);
    }
  };

  return (
    <>
      <header className="app-header">
        <div className="header-left">
          <img src="/img/c3po_icon.png" alt="C3PO" className="header-icon" />
          <div>
            <h1 className="header-title">C3PO</h1>
            <span className="header-subtitle">ONC Analytics Assistant</span>
          </div>
        </div>
        <div className="header-right">
          <button
            className="narrative-deck-btn"
            onClick={handleDeckDownload}
            disabled={deckLoading}
            title="Download latest Narrative Deck"
          >
            {deckLoading ? (
              <Spinner size={14} />
            ) : (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M8 1v10M8 11L4.5 7.5M8 11l3.5-3.5M2 13h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            )}
            <span>Narrative Deck</span>
          </button>
          {deckError && <span className="deck-error-toast">{deckError}</span>}
          <button
            className="readme-btn"
            onClick={handleReadme}
            title="About C3PO"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 16v-4" />
              <path d="M12 8h.01" />
            </svg>
            <span>README</span>
          </button>
          <button
            className="refresh-templates-btn"
            onClick={handleRefresh}
            disabled={refreshing}
            title="Reload templates from database"
          >
            {refreshing ? (
              <Spinner size={14} />
            ) : (
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M1 8a7 7 0 0112.9-3.8M15 8a7 7 0 01-12.9 3.8M1 4.2V1m0 3.2H4.2M15 11.8V15m0-3.2H11.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            )}
          </button>
          {refreshMsg && <span className="refresh-toast">{refreshMsg}</span>}
        </div>
      </header>

      {/* README Modal */}
      {readmeOpen && (
        <div className="readme-overlay" onClick={() => setReadmeOpen(false)}>
          <div className="readme-modal" onClick={(e) => e.stopPropagation()}>
            <div className="readme-modal-header">
              <h2>About C3PO</h2>
              <button className="readme-close-btn" onClick={() => setReadmeOpen(false)}>&times;</button>
            </div>
            <div className="readme-modal-body">
              {readmeLoading ? (
                <div className="readme-loading"><Spinner size={20} /> Loading...</div>
              ) : (
                <div
                  className="readme-content"
                  dangerouslySetInnerHTML={{ __html: renderMarkdownSimple(readmeContent) }}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** Markdown to HTML renderer with table, link, and blockquote support */
function renderMarkdownSimple(md: string): string {
  const lines = md.split("\n");
  const html: string[] = [];
  let inTable = false;
  let inList = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      if (inList) { html.push("</ul>"); inList = false; }
      if (inTable) { html.push("</tbody></table>"); inTable = false; }
      html.push("<hr/>");
      continue;
    }

    // Table row
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      // Skip separator row (|---|---|)
      if (/^[\s|\-:]+$/.test(line.replace(/\|/g, "").trim() + "-")) {
        continue;
      }
      const cells = line.split("|").filter((c, idx, arr) => idx > 0 && idx < arr.length - 1);
      if (!inTable) {
        if (inList) { html.push("</ul>"); inList = false; }
        html.push("<table><thead><tr>");
        cells.forEach(c => html.push(`<th>${formatInline(c.trim())}</th>`));
        html.push("</tr></thead><tbody>");
        inTable = true;
        // Skip separator line
        if (i + 1 < lines.length && /^[\s|\-:]+$/.test(lines[i + 1])) i++;
      } else {
        html.push("<tr>");
        cells.forEach(c => html.push(`<td>${formatInline(c.trim())}</td>`));
        html.push("</tr>");
      }
      continue;
    } else if (inTable) {
      html.push("</tbody></table>");
      inTable = false;
    }

    // Headers
    if (line.startsWith("### ")) { if (inList) { html.push("</ul>"); inList = false; } html.push(`<h3>${formatInline(line.slice(4))}</h3>`); continue; }
    if (line.startsWith("## ")) { if (inList) { html.push("</ul>"); inList = false; } html.push(`<h2>${formatInline(line.slice(3))}</h2>`); continue; }
    if (line.startsWith("# ")) { if (inList) { html.push("</ul>"); inList = false; } html.push(`<h1>${formatInline(line.slice(2))}</h1>`); continue; }

    // Blockquote
    if (line.startsWith(">")) {
      if (inList) { html.push("</ul>"); inList = false; }
      html.push(`<blockquote>${formatInline(line.slice(1).trim())}</blockquote>`);
      continue;
    }

    // List item
    if (/^[-*] /.test(line.trim())) {
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push(`<li>${formatInline(line.trim().slice(2))}</li>`);
      continue;
    } else if (inList) {
      html.push("</ul>"); inList = false;
    }

    // Empty line = paragraph break
    if (line.trim() === "") {
      html.push("<br/>");
      continue;
    }

    // Regular paragraph
    html.push(`<p>${formatInline(line)}</p>`);
  }

  if (inList) html.push("</ul>");
  if (inTable) html.push("</tbody></table>");

  return html.join("\n");
}

function formatInline(text: string): string {
  // Convert markdown links [text](url) — trim whitespace from URL
  let result = text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(\s*([^)]+?)\s*\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Auto-link bare URLs only if not already inside an <a> tag (check not preceded by " or = or >)
  if (!result.includes("<a ")) {
    result = result.replace(/(https?:\/\/[^\s<"]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }
  return result;
}
