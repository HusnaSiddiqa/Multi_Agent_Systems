/**
 * C3PO v3 - Main Application
 * Premium chatbot UI with appkit visualization
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { Header } from "./components/Header";
import { ThreadsPanel } from "./components/ThreadsPanel";
import { ChatArea } from "./components/ChatArea";
import { ChatInput } from "./components/ChatInput";
import { submitQuestion, pollProgress, fetchHistory, fetchThreadMessages } from "./api";
import type { ChatMessage, Suggestion, Thread } from "./types";

export function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [currentThreadId, setCurrentThreadId] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollCountRef = useRef(0);
  const messagesRef = useRef<ChatMessage[]>(messages);
  messagesRef.current = messages;
  const fetchingThreadRef = useRef<string | null>(null); // prevent concurrent thread fetches

  const loadHistory = useCallback(async () => {
    try {
      const data = await fetchHistory();
      setThreads(data.map(t => ({
        id: t.thread_id,
        title: t.title || "Untitled",
        timestamp: new Date((t.created_at || "").replace(" ", "T").replace(/(\\.\\d{3})\\d+/, "$1")).getTime() || Date.now(),
        messageCount: 0,
      })));
    } catch {
      // history is non-critical
    }
  }, []);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  const handleNewChat = () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    setMessages([]);
    setIsProcessing(false);
    setCurrentThreadId(null);
  };

  const handleThreadClick = useCallback(async (threadId: string) => {
    if (isProcessing) return;
    if (fetchingThreadRef.current === threadId) return; // already fetching this thread
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    setCurrentThreadId(threadId);
    setMessages([]);
    fetchingThreadRef.current = threadId;
    try {
      const msgs = await fetchThreadMessages(threadId);
      const chatMessages: ChatMessage[] = msgs
        .filter(m => {
          // Keep any message with a question (user) or answer/payload (assistant)
          if (m.role === "user") return !!m.question;
          return !!(m.answer || m.payload);
        })
        .map(m => {
          const ts = m.created_at
            ? new Date(m.created_at.replace(" ", "T").replace(/(\\.\\d{3})\\d+/, "$1")).getTime() || Date.now()
            : Date.now();
          if (m.role === "user") {
            return {
              id: m.message_id,
              role: "user" as const,
              content: m.question || "",
              timestamp: ts,
            };
          } else {
            let result = undefined;
            if (m.payload) { try { result = JSON.parse(m.payload); } catch { /* ignore */ } }
            return {
              id: m.message_id,
              messageId: m.message_id,  // same value — makes feedback lookup explicit
              threadId: m.thread_id,
              userQuestion: m.question || "",
              role: "assistant" as const,
              content: m.answer || "",
              timestamp: ts,
              path: (m.path as "click" | "semantic") || "semantic",
              result,
              feedbackRating: (m.feedback_rating as "positive" | "negative" | null) ?? null,
              feedbackCategory: (m.feedback_request as string | null) ?? null,
              feedbackComment: (m.feedback_comment as string | null) ?? null,
            };
          }
        });
      setMessages(chatMessages);
    } catch (err) {
      console.error("[handleThreadClick] failed:", err);
      setMessages([]);
    } finally {
      fetchingThreadRef.current = null;
    }
  }, [isProcessing]);

  // Build context string for follow-up resolution (last 4 messages = last 2 turns)
  const buildContext = (msgs: ChatMessage[]): string => {
    const turns = msgs
      .filter((m) => !m.isLoading)
      .slice(-4);
    return turns
      .map((m) => {
        if (m.role === "user") return `User: ${m.content}`;
        if (m.result?.success && m.result.row_count) {
          // Template path: include original question + metric + slots for full context
          const question = m.userQuestion || "";
          const metric = m.result.metric_name || "";
          const slots = m.result.slot_values
            ? Object.entries(m.result.slot_values).map(([k, v]) => `${k}=${v}`).join(", ")
            : "";
          const metricPart = `${metric}${slots ? ` (${slots})` : ""}`;
          return question
            ? `Assistant: [answered "${question}"] ${metricPart}`
            : `Assistant: ${metricPart}`;
        }
        if (m.result?.success && m.result.insight) {
          // Supervisor text-only answer: include question context + insight
          const question = m.userQuestion || "";
          const insightPreview = (m.result.insight as string || "").slice(0, 200);
          return question
            ? `Assistant: [answered "${question}"] ${insightPreview}`
            : `Assistant: ${insightPreview}`;
        }
        if (m.content) {
          // Fallback: message loaded from DB history — use stored answer text
          return `Assistant: ${m.content}`;
        }
        return null;
      })
      .filter(Boolean)
      .join("\n");
  };

  const handleSubmit = useCallback(
    async (question: string, suggestion?: Suggestion) => {
      if (isProcessing || !question.trim()) return;

      const userMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: question,
        timestamp: Date.now(),
      };

      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        timestamp: Date.now(),
        isLoading: true,
        path: suggestion?.data ? "click" : "semantic",
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setIsProcessing(true);

      try {
        const context = buildContext(messagesRef.current);
        const { session_id, thread_id } = await submitQuestion(
          question,
          suggestion?.data as Record<string, unknown> | undefined,
          context,
          currentThreadId
        );
        setCurrentThreadId(thread_id);
        setThreads(prev => {
          if (prev.some(t => t.id === thread_id)) return prev;
          return [{ id: thread_id, title: question, timestamp: Date.now(), messageCount: 0 }, ...prev];
        });
        loadHistory();
        assistantMsg.sessionId = session_id;

        pollCountRef.current = 0;
        pollingRef.current = setInterval(async () => {
          pollCountRef.current += 1;
          if (pollCountRef.current > 450) {
            clearInterval(pollingRef.current!);
            pollingRef.current = null;
            setMessages((prev) => prev.map((m) =>
              m.id === assistantMsg.id
                ? { ...m, isLoading: false, result: { success: false, error_msg: "Request timed out. Please try again." } }
                : m
            ));
            setIsProcessing(false);
            return;
          }
          try {
            const progress = await pollProgress(session_id);
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? {
                      ...m,
                      path: progress.path,
                      result: progress.result || undefined,
                      isLoading: !progress.done && !progress.result,
                      insightPending: !progress.done && !!progress.result,
                      _steps: progress.steps,
                      ...(progress.done && (progress.result as any)?.message_id
                        ? {
                            messageId: (progress.result as any).message_id as string,
                            threadId: thread_id,
                            userQuestion: progress.question || question,
                            resolvedQuestion: (progress as any).resolved_question || undefined,
                          }
                        : {}),
                    }
                  : m
              )
            );
            if (progress.done) {
              clearInterval(pollingRef.current!);
              pollingRef.current = null;
              setIsProcessing(false);
              loadHistory();
            }
          } catch {
            clearInterval(pollingRef.current!);
            pollingRef.current = null;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? { ...m, isLoading: false, result: { success: false, error_msg: "Connection lost. Please try again." } }
                  : m
              )
            );
            setIsProcessing(false);
          }
        }, 400);
      } catch (err: unknown) {
        const errMsg = err instanceof Error ? err.message : "Failed to submit";
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsg.id
              ? { ...m, isLoading: false, result: { success: false, error_msg: errMsg } }
              : m
          )
        );
        setIsProcessing(false);
      }
    },
    [currentThreadId, isProcessing, loadHistory]
  );

  const handleChipClick = useCallback((question: string) => {
    handleSubmit(question);
  }, [handleSubmit]);

  return (
    <div className="app-root">
      <Header />
      <div className="app-body">
        <ThreadsPanel
          threads={threads}
          activeThread={currentThreadId ?? ""}
          onNewChat={handleNewChat}
          onThreadClick={handleThreadClick}
        />
        <div className="chat-panel">

          <ChatArea messages={messages} onChipClick={handleChipClick} />
          <ChatInput
            onSubmit={handleSubmit}
            isProcessing={isProcessing}
          />
        </div>
      </div>
    </div>
  );
}
