/**
 * C3PO v3 - API Client
 * Clean abstraction over Flask backend endpoints
 */

import type { Suggestion, ProgressResponse } from "./types";

const API_BASE = "";

/**
 * Fetch typeahead suggestions for partial input
 */
export async function fetchSuggestions(query: string): Promise<Suggestion[]> {
  if (query.trim().length < 2) return [];
  
  const resp = await fetch(
    `${API_BASE}/api/suggestions?q=${encodeURIComponent(query)}`
  );
  if (!resp.ok) return [];
  return resp.json();
}

/**
 * Submit a question for processing
 * Returns session_id for progress polling
 */
export async function submitQuestion(
  question: string,
  templateRow?: Record<string, unknown>,
  context?: string,
  threadId?: string | null
): Promise<{ session_id: string; path: string; thread_id: string }> {
  const resp = await fetch(`${API_BASE}/api/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      template_row: templateRow || null,
      context: context || "",
      thread_id: threadId || null,
    }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: "Request failed" }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }

  return resp.json();
}

/**
 * Fetch thread history list for the current user
 */
export async function fetchHistory(): Promise<
  Array<{ thread_id: string; title: string; created_at: string }>
> {
  const resp = await fetch(`${API_BASE}/api/history`);
  if (!resp.ok) return [];
  return resp.json();
}

/**
 * Fetch all messages for a thread
 */
export async function fetchThreadMessages(threadId: string): Promise<
  Array<{
    thread_id: string;
    message_id: string;
    role: string;
    question: string | null;
    answer: string | null;
    path: string | null;
    payload: string | null;
    created_at: string;
    feedback_rating: string | null;
    feedback_request: string | null;
    feedback_comment: string | null;
  }>
> {
  const resp = await fetch(`${API_BASE}/api/thread/${threadId}/messages`);
  if (!resp.ok) return [];
  return resp.json();
}

/**
 * Submit user feedback for a specific assistant message.
 * rating: 'positive' | 'negative'
 * category: optional chip label (negative panel)
 * comment: optional free-text
 */
export async function submitFeedback(
  messageId: string,
  rating?: "positive" | "negative" | null,
  category?: string,
  comment?: string
): Promise<void> {
  await fetch(`${API_BASE}/api/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message_id: messageId, rating, category, comment }),
  });
  // Fire-and-forget: do not throw — feedback failure must never block the UI
}

/**
 * Poll for processing progress
 */
export async function pollProgress(sessionId: string): Promise<ProgressResponse> {
  const resp = await fetch(`${API_BASE}/api/progress/${sessionId}`);
  
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: "Poll failed" }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  
  return resp.json();
}

/**
 * Generate PPT and trigger direct download.
 * Returns the file as a Blob, triggers browser download.
 */
export async function generatePpt(params: {
  message_id: string;
  thread_id: string;
  statement_response: Record<string, unknown>;
  sql?: string;
  question?: string;
  metric_name?: string;
  insight?: string;
}): Promise<{ success: boolean; error?: string }> {
  const resp = await fetch(`${API_BASE}/api/generate-ppt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });

  if (!resp.ok) {
    // Backend returned JSON error
    const err = await resp.json().catch(() => ({ error: "Generation failed" }));
    return { success: false, error: err.error || `HTTP ${resp.status}` };
  }

  // Success — response is the .pptx binary file
  const blob = await resp.blob();
  const filename = resp.headers.get("content-disposition")
    ?.match(/filename="?([^"]+)"?/)?.[1] || `c3po_chart_${params.message_id.slice(0, 8)}.pptx`;

  // Trigger browser download
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  return { success: true };
}


export async function downloadNarrativeDeck(): Promise<void> {
  const resp = await fetch("/api/download-narrative-deck");
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: "Download failed" }));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const filename = resp.headers.get("content-disposition")?.match(/filename="?([^"]+)"?/)?.[1] || "narrative_deck.pptx";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function refreshTemplates(): Promise<{ success: boolean; total_templates?: number; active_templates?: number; error?: string }> {
  const resp = await fetch("/api/admin/refresh-templates", { method: "POST" });
  return resp.json();
}


/**
 * Download query result as Excel file
 */
export async function downloadExcel(params: {
  sql?: string;
  statement_response?: Record<string, unknown>;
  question?: string;
}): Promise<{ success: boolean; error?: string }> {
  const resp = await fetch(`${API_BASE}/api/download-excel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: "Download failed" }));
    return { success: false, error: err.error || `HTTP ${resp.status}` };
  }

  const blob = await resp.blob();
  const filename = resp.headers.get("content-disposition")
    ?.match(/filename="?([^"]+)"?/)?.[1] || "c3po_data.xlsx";

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  return { success: true };
}

/**
 * Fetch README content
 */
export async function fetchReadme(): Promise<string> {
  const resp = await fetch(`${API_BASE}/api/readme`);
  if (!resp.ok) return "# Error loading README";
  const data = await resp.json();
  return data.content || "";
}
