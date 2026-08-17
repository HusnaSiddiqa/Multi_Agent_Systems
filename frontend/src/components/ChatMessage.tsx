/**
 * Individual chat message - handles user and assistant messages
 */

import { useState } from "react";
import { ProgressSteps } from "./ProgressSteps";
import { QueryVisualization } from "./QueryVisualization";
import { submitFeedback, generatePpt, downloadExcel } from "../api";
import type { ChatMessage as ChatMessageType, FeedbackRating } from "../types";
import { Spinner } from "./Spinner";

// ============================================================
// FeedbackBar component
// TODO: move to its own FeedbackBar.tsx when workspace tools allow
// creating files in Prathyusha's directory directly.
// ============================================================

const IconThumbUp = ({ filled }: { filled?: boolean }) => (
  <svg width="15" height="15" viewBox="0 0 24 24"
    fill={filled ? "currentColor" : "none"}
    stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M7 10v12" />
    <path d="M15 5.88L14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" />
  </svg>
);

const IconThumbDown = ({ filled }: { filled?: boolean }) => (
  <svg width="15" height="15" viewBox="0 0 24 24"
    fill={filled ? "currentColor" : "none"}
    stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M17 14V2" />
    <path d="M9 18.12L10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L13 22a3.13 3.13 0 0 1-3-3.88Z" />
  </svg>
);

const IconComment = ({ filled }: { filled?: boolean }) => (
  <svg width="15" height="15" viewBox="0 0 24 24"
    fill={filled ? "currentColor" : "none"}
    stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
  </svg>
);

const IconRefresh = () => (
  <svg width="13" height="13" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="23 4 23 10 17 10" />
    <polyline points="1 20 1 14 7 14" />
    <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
  </svg>
);

const NEGATIVE_CHIPS = [
  "Values look off",
  "Missing expected columns",
  "Genie made up a definition",
  "Wrong visualization",
] as const;

// Drop-in replacement for the FeedbackBar function in ChatMessage.tsx.
// Icon components (IconThumbUp, etc.), NEGATIVE_CHIPS, imports, and types
// stay exactly as they are. Only this function changes.
//
// Behavior contract:
//   - Positive  : commits immediately, NEVER locks. Switching away (or clicking
//                 the thumb again) retracts it from the backend, so an accidental
//                 positive is never left as the final stored value.
//   - Negative  : commits on Submit, then locks the row (goes gray).
//   - Review    : commits on Send, then locks the row (goes gray).

type FeedbackPanel = "none" | "negative" | "review";

function FeedbackBar({
  messageId,
  initialRating = null,
  initialCategory = null,
  initialComment = null,
}: {
  messageId?: string;
  initialRating?: FeedbackRating;
  initialCategory?: string | null;
  initialComment?: string | null;
}) {
  const [rating, setRating] = useState<FeedbackRating>(initialRating);
  const [panel, setPanel] = useState<FeedbackPanel>("none");
  const [selectedChip, setSelectedChip] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [reviewComment, setReviewComment] = useState(initialComment ?? "");

  const wasReviewRequested = initialCategory === "Review Requested";

  // negative OR a previously-sent review request both lock the row on reload
  const [submitted, setSubmitted] = useState(initialRating === "negative" || wasReviewRequested);
  const [confirmMsg, setConfirmMsg] = useState<string | null>(
    initialRating === "positive"
      ? "Feedback recorded — you can still change it."
      : initialRating === "negative"
      ? "Feedback submitted — thanks for helping us improve."
      : wasReviewRequested
      ? "Review request submitted."
      : null
  );

  if (!messageId) return null;

  // If a positive rating was already written to the backend and the user is now
  // switching to negative / review, clear it first so positive is never the
  // final stored value. Maps to feedback_rating = NULL in save_feedback().
  const retractPositive = () => {
    if (rating === "positive") {
      submitFeedback(messageId, null);
    }
  };

  // Positive: commit immediately, but NEVER lock. Clicking again clears it.
  const handleThumbUp = () => {
    if (submitted) return;
    if (rating === "positive") {
      // toggle off — retract the positive we just wrote
      setRating(null);
      setConfirmMsg(null);
      submitFeedback(messageId, null);
      return;
    }
    setRating("positive");
    setPanel("none");
    setConfirmMsg("Feedback recorded — you can still change it.");
    submitFeedback(messageId, "positive");
  };

  const handleThumbDown = () => {
    if (submitted) return;
    setConfirmMsg(null);
    if (panel === "negative") {
      // second click closes the panel
      setPanel("none");
      setRating(null);
      return;
    }
    retractPositive();           // leaving positive -> clear it in the backend
    setRating("negative");
    setPanel("negative");
  };

  const handleReviewIcon = () => {
    if (submitted) return;
    setConfirmMsg(null);
    if (panel === "review") {
      setPanel("none");
      return;
    }
    retractPositive();           // leaving positive -> clear it in the backend
    setRating(null);
    setSelectedChip(null);
    setComment("");
    setPanel("review");
  };

  // Finalizing actions — these DO lock the row (turns gray, no further edits).
  const handleNegativeSubmit = () => {
    setConfirmMsg("Feedback submitted — thanks for helping us improve.");
    setSubmitted(true);
    setPanel("none");
    const combinedComment = [selectedChip, comment.trim()].filter(Boolean).join(" — ");
    submitFeedback(messageId, "negative", undefined, combinedComment || undefined);
  };
  
  const handleReviewSubmit = () => {
    setConfirmMsg("Review request submitted.");
    setSubmitted(true);
    setPanel("none");
    submitFeedback(messageId, undefined, "Review Requested", reviewComment.trim() || undefined);
  };

  return (
    <div className="feedback-bar">
      <div className="feedback-row">
        <span className="feedback-label">Is this useful?</span>

        <button
          className={`feedback-btn${rating === "positive" ? " active positive" : ""}${submitted && rating !== "positive" ? " locked" : ""}`}
          onClick={handleThumbUp}
          disabled={submitted}
          aria-label="Thumbs up — helpful"
          title="Yes, helpful"
        >
          <IconThumbUp filled={rating === "positive"} />
        </button>

        <button
          className={`feedback-btn${panel === "negative" || rating === "negative" ? " active negative" : ""}${submitted && rating !== "negative" ? " locked" : ""}`}
          onClick={handleThumbDown}
          disabled={submitted}
          aria-label="Thumbs down — not helpful"
          title="No, not helpful"
        >
          <IconThumbDown filled={rating === "negative"} />
        </button>

        <button
          className={`feedback-btn${panel === "review" ? " active" : ""}${submitted ? " locked" : ""}`}
          onClick={handleReviewIcon}
          disabled={submitted}
          aria-label="Add a comment"
          title="Add a comment"
        >
          <IconComment filled={panel === "review"} />
        </button>

        {confirmMsg && <span className="feedback-confirm">{confirmMsg}</span>}
      </div>

      {panel === "negative" && !submitted && (
        <div className="feedback-panel">
          <div className="feedback-chips">
            {NEGATIVE_CHIPS.map(chip => (
              <button
                key={chip}
                className={`feedback-chip${selectedChip === chip ? " selected" : ""}`}
                onClick={() => setSelectedChip(prev => (prev === chip ? null : chip))}
              >
                {chip}
              </button>
            ))}
          </div>
          <textarea
            className="feedback-textarea"
            placeholder="(Optional) Explain what is wrong with the response"
            value={comment}
            onChange={e => setComment(e.target.value)}
            rows={2}
          />
          <div className="feedback-actions">
            <button className="feedback-submit-retry" onClick={handleNegativeSubmit}>
              Submit
            </button>
          </div>
        </div>
      )}

      {panel === "review" && !submitted && (
        <div className="feedback-panel">
          <textarea
            className="feedback-textarea"
            placeholder="Add a comment for the agent manager (optional)"
            value={reviewComment}
            onChange={e => setReviewComment(e.target.value)}
            rows={2}
          />
          <div className="feedback-actions">
            <button className="feedback-cancel" onClick={() => setPanel("none")}>Cancel</button>
            <button className="feedback-submit-retry" onClick={handleReviewSubmit}>Send for review</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================

/** Lightweight markdown to HTML for supervisor insights */
function renderMarkdown(text: string): string {
  if (!text) return "";
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^## (.+)$/gm, "<h3>$1</h3>")
    .replace(/^# (.+)$/gm, "<h2>$1</h2>");
  const lines = html.split("\n");
  const out: string[] = [];
  let inList = false;
  for (const line of lines) {
    const trimmed = line.trim();
    const bullet = trimmed.match(/^[-•]\s+(.*)/);
    if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + bullet[1] + "</li>");
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      if (trimmed.startsWith("<h")) {
        out.push(trimmed);
      } else if (trimmed) {
        out.push("<p>" + trimmed + "</p>");
      }
    }
  }
  if (inList) out.push("</ul>");
  return out.join("");
}

interface Props {
  message: ChatMessageType;
}

export function ChatMessage({ message }: Props) {
  const { role, content, path, result, isLoading } = message;
  const source = result?.source;
  const [showSql, setShowSql] = useState(false);
  const [pptState, setPptState] = useState<"idle" | "loading" | "error">("idle");
  const [excelLoading, setExcelLoading] = useState(false);
  const [pptError, setPptError] = useState<string | null>(null);
  const [showSqlMap, setShowSqlMap] = useState<Record<number, boolean>>({});
  const fallbackContent = content || "";
  const shouldRenderFallbackContent = Boolean(
    !isLoading &&
    fallbackContent &&
    (
      !result ||
      (!result.success && !result.error_msg) ||
      (result.success && !result.insight && !result.statement_response)
    )
  );

  if (role === "user") {
    return (
      <div className="msg-row user">
        <div className="msg-bubble user-bubble">
          <p>{content}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="msg-row assistant">
      <div className="msg-bubble assistant-bubble">
        {/* Path + source badges — mutually exclusive: show Trusted OR Supervisor, never both */}
        <div className="badge-row">
          {path && source !== "supervisor" && (
            <span className={`path-badge ${path}`}>
              {path === "click" ? "⚡ Direct Match - Trusted" : "🔍 Semantic Search - Trusted"}
            </span>
          )}
          {source === "supervisor" && (
            <span className="path-badge supervisor">🤖 Supervisor Agent</span>
          )}
        </div>
        {/* Revised question — show context resolution result */}
        {message.resolvedQuestion && (
          <div className="revised-question">
            <span className="revised-label">Revised query:</span> {message.resolvedQuestion}
          </div>
        )}

        {/* Genie spaces — show all calls in order, fallback to legacy single-space */}
        {result?.supervisor_meta?.genie_calls && result.supervisor_meta.genie_calls.length > 0 ? (
          <div className="genie-space-line">
            <span className="genie-space-label">🧞 Genie Spaces:</span>
            <span className="genie-space-name">
              {result.supervisor_meta.genie_calls.map(c => c.genie_space_name).join("  →  ")}
            </span>
          </div>
        ) : result?.supervisor_meta?.genie_space_name ? (
          <div className="genie-space-line">
            <span className="genie-space-label">🧞 Genie Space:</span>
            <span className="genie-space-name">{result.supervisor_meta.genie_space_name}</span>
          </div>
        ) : null}

        {isLoading && (
          <ProgressSteps
            steps={(message as unknown as { _steps?: Array<{ name: string; details?: string }> })._steps || []}
            path={path}
          />
        )}

        {/* Fallback for restored history rows when payload is missing, malformed, or incomplete */}
        {shouldRenderFallbackContent && (
          <div className="insight-box">
            {fallbackContent.includes("**") ? (
              <div
                className="insight-content"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(fallbackContent) }}
              />
            ) : (
              <p>{fallbackContent}</p>
            )}
          </div>
        )}

        {!isLoading && result && !result.success && (
          <div className="error-container">
            <div className="error-icon">⚠️</div>
            <p className="error-text">{result.error_msg || "An error occurred"}</p>
          </div>
        )}

        {!isLoading && result?.success && (
          <div className="result-container">
            {result.metric_name && (
              <div className="result-summary">
                <span className="metric-name">{result.metric_name}</span>
                {result.slot_values && Object.keys(result.slot_values).length > 0 && (
                  <span className="slot-pills">
                    {Object.entries(result.slot_values).map(([k, v]) => (
                      <span key={k} className="slot-pill">{String(v)}</span>
                    ))}
                  </span>
                )}
                <span className="row-count">{result.row_count} rows</span>
                <span className="timing">{result.total_time}s</span>
              </div>
            )}

            {result.statement_response && (
              <QueryVisualization data={result.statement_response} />
            )}

            {/* Export to PPT button - shown when data is available */}
            {result.statement_response && (
              <div className="ppt-export-row">
                {pptState === "idle" && (
                  <button
                    className="ppt-export-btn"
                    onClick={async () => {
                      setPptState("loading");
                      setPptError(null);
                      try {
                        const resp = await generatePpt({
                          message_id: message.messageId || "",
                          thread_id: message.threadId || "",
                          statement_response: result.statement_response as Record<string, unknown>,
                          sql: result.sql || "",
                          question: message.userQuestion || result?.metric_name || "",
                          metric_name: result.metric_name || "",
                          insight: result.insight || "",
                        });
                        if (resp.success) {
                          setPptState("idle");
                        } else {
                          setPptError(resp.error || "Generation failed");
                          setPptState("error");
                        }
                      } catch (e: unknown) {
                        setPptError(e instanceof Error ? e.message : "Unknown error");
                        setPptState("error");
                      }
                    }}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                      <polyline points="7 10 12 15 17 10" />
                      <line x1="12" y1="15" x2="12" y2="3" />
                    </svg>
                    Export to PPT
                  </button>
                )}
                {pptState === "loading" && (
                  <div className="ppt-loading">
                    <Spinner size={14} />
                    <span>Generating chart...</span>
                  </div>
                )}
                {pptState === "error" && (
                  <div className="ppt-error">
                    <span>⚠️ {pptError}</span>
                    <button className="ppt-retry-btn" onClick={() => setPptState("idle")}>Retry</button>
                  </div>
                )}
              </div>
            )}

            {/* Download Excel button */}
            {result.statement_response && (
              <div className="excel-export-row">
                {!excelLoading ? (
                  <button
                    className="excel-export-btn"
                    onClick={async () => {
                      setExcelLoading(true);
                      try {
                        await downloadExcel({
                          sql: result.sql || "",
                          statement_response: result.statement_response as Record<string, unknown>,
                          question: message.userQuestion || result?.metric_name || "",
                        });
                      } finally {
                        setExcelLoading(false);
                      }
                    }}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                      <line x1="16" y1="13" x2="8" y2="13" />
                      <line x1="16" y1="17" x2="8" y2="17" />
                    </svg>
                    Download Excel
                  </button>
                ) : (
                  <div className="excel-loading">
                    <Spinner size={14} />
                    <span>Downloading...</span>
                  </div>
                )}
              </div>
            )}

            {/* Insight - render markdown for supervisor, stream for template */}
            {result.insight && (
              <div className={`insight-box${message.insightPending ? " streaming" : ""}`}>
                <span className="insight-icon">💡</span>
                {result.insight.includes("**") ? (
                  <div
                    className="insight-content"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(result.insight) }}
                  />
                ) : (
                  <p>{result.insight}</p>
                )}
                {message.insightPending && <span className="streaming-cursor" />}
              </div>
            )}
            {/* Show generating indicator before first token arrives */}
            {!result.insight && message.insightPending && source !== "supervisor" && (
              <div className="insight-pending">
                <Spinner size={14} />
                <span>Generating insight...</span>
              </div>
            )}

            {/* SQL toggles: one per Genie call for supervisor path; single toggle for template */}
            {result.supervisor_meta?.genie_calls && result.supervisor_meta.genie_calls.length > 0
              ? result.supervisor_meta.genie_calls.map((call, i) =>
                  call.sql ? (
                    <div key={i} className="sql-section">
                      <button
                        className="sql-toggle"
                        onClick={() => setShowSqlMap(prev => ({ ...prev, [i]: !prev[i] }))}
                      >
                        <span>{showSqlMap[i] ? "▼" : "▶"}</span>
                        <span>
                          {result.supervisor_meta!.genie_calls!.length > 1
                            ? `SQL — ${call.genie_space_name}`
                            : "SQL Query"}
                        </span>
                      </button>
                      {showSqlMap[i] && <pre className="sql-code">{call.sql}</pre>}
                    </div>
                  ) : null
                )
              : result.sql && (
                  <div className="sql-section">
                    <button className="sql-toggle" onClick={() => setShowSql(!showSql)}>
                      <span>{showSql ? "▼" : "▶"}</span>
                      <span>SQL Query</span>
                    </button>
                    {showSql && <pre className="sql-code">{result.sql}</pre>}
                  </div>
                )}

            {result.steps_log && result.steps_log.length > 0 && (
              <details className="steps-details">
                <summary>⚙️ Steps ({result.total_time}s)</summary>
                <div className="steps-content">
                  {result.steps_log.map((step, i) => (
                    <div key={i} className="step-item">
                      <span className="step-dot-done" />
                      <span className="step-text">
                        {step.name}: {step.details} ({step.duration}s)
                      </span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}

        {/* Feedback bar — shown after every completed assistant response */}
        {!isLoading && (
          <FeedbackBar
            messageId={message.messageId}
            initialRating={message.feedbackRating ?? null}
            initialCategory={message.feedbackCategory ?? null}
            initialComment={message.feedbackComment ?? null}
          />
        )}
      </div>
    </div>
  );
}
