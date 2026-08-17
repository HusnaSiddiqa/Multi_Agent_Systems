/**
 * C3PO v3 - Shared TypeScript interfaces
 */

// GenieStatementResponse shape (for appkit-ui visualization)
export interface StatementResponse {
  manifest: {
    schema: {
      columns: Array<{ name: string; type_name: string }>;
    };
  };
  result: {
    data_array: (string | null)[][];
  };
}

// A single processing step
export interface ProcessingStep {
  name: string;
  details?: string;
  duration?: number;
  time?: number;
}

// API response from /api/progress
export interface ProgressResponse {
  steps: ProcessingStep[];
  done: boolean;
  path: "click" | "semantic";
  question: string;
  result?: QueryResult;
}

// A single Genie call made by the supervisor (one entry per space, after dedup)
export interface GenieCall {
  space: string;              // genie function name e.g. "genie-01f1527e..."
  genie_space_id?: string;
  genie_space_name: string;   // human-readable space name
  query?: string;             // question sent to Genie
  sql?: string;               // final SQL from trace (one per call, last refinement wins)
}

// The final query result
export interface QueryResult {
  success: boolean;
  statement_response?: StatementResponse;
  all_statement_responses?: StatementResponse[];
  insight?: string;
  sql?: string;
  all_sqls?: string[];
  slot_values?: Record<string, string>;
  metric_name?: string;
  answer_type?: string;
  total_time?: number;
  steps_log?: ProcessingStep[];
  error_msg?: string;
  row_count?: number;
  source?: "template" | "supervisor";   // which path answered
  answer?: string;                       // supervisor text answer
  supervisor_meta?: {
    genie_calls?: GenieCall[];           // ordered list, one entry per Genie space called
    genie_space?: string;                // legacy: genie function name (last call)
    genie_space_name?: string;           // legacy: human-readable name (last call)
    genie_query?: string;                // legacy: question sent to Genie
  };
}

// A suggestion item from typeahead
export interface Suggestion {
  label: string;
  data: Record<string, unknown>;
}

// Feedback state for a single assistant message
export type FeedbackRating = "positive" | "negative" | null;

// Chat message (in frontend state)
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content?: string;
  timestamp: number;
  // Assistant-specific fields
  path?: "click" | "semantic";
  result?: QueryResult;
  isLoading?: boolean;
  /** True when table/chart is ready but insight is still generating */
  insightPending?: boolean;
  sessionId?: string;
  /** Actual DB message_id — set when poll completes (live) or from history load. Used for feedback API calls. */
  messageId?: string;
  /** Thread ID for this message - needed for PPT export */
  /** Original user question that triggered this assistant response - used for PPT chart context */
  userQuestion?: string;
  resolvedQuestion?: string;
  /** Pre-existing feedback rating loaded from history */
  feedbackRating?: FeedbackRating;
  feedbackCategory?: string | null;
  feedbackComment?: string | null;
}

// Thread (for sidebar - placeholder)
export interface Thread {
  id: string;
  title: string;
  timestamp: number;
  messageCount: number;
}
