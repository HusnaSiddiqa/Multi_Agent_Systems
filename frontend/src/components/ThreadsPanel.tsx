/**
 * Threads sidebar — last 7 days of conversation history per user
 */

import type { Thread } from "../types";

interface Props {
  threads: Thread[];
  activeThread: string;
  onNewChat?: () => void;
  onThreadClick?: (threadId: string) => void;
}

function formatDate(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const diffDays = Math.floor((now.getTime() - d.getTime()) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function ThreadsPanel({ threads, activeThread, onNewChat, onThreadClick }: Props) {
  return (
    <aside className="threads-panel">
      <div className="threads-header">
        <h3>THREADS</h3>
        <button className="new-thread-btn" title="New chat" onClick={onNewChat}>
          <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
      </div>
      <div className="threads-list">
        {threads.length === 0 && (
          <div className="threads-empty">No recent conversations</div>
        )}
        {threads.map((thread) => (
          <div
            key={thread.id}
            className={`thread-item ${thread.id === activeThread ? "active" : ""}`}
            onClick={() => onThreadClick?.(thread.id)}
          >
            <div className="thread-title">{thread.title}</div>
            <div className="thread-meta">{formatDate(thread.timestamp)}</div>
          </div>
        ))}
      </div>
    </aside>
  );
}
