/**
 * Chat display area with auto-scroll
 */

import { useEffect, useRef } from "react";
import { ChatMessage } from "./ChatMessage";
import type { ChatMessage as ChatMessageType } from "../types";

interface Props {
  messages: ChatMessageType[];
  onChipClick: (question: string) => void;
}

export function ChatArea({ messages, onChipClick }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="chat-area">
        <div className="welcome-screen">
          <img src="/img/c3po_icon.png" alt="C3PO" className="welcome-icon" />
          <h2>Welcome to C3PO</h2>
          <p>Ask about ONCOLOGY Sales & Claims, Prescription, NPS/TPS shares, trends, and market analytics.</p>
          <div className="welcome-examples">
            <span className="example-chip" onClick={() => onChipClick("How are TRO weekly sales trend?")}>
              How are TRO weekly sales trend?
            </span>
            <span className="example-chip" onClick={() => onChipClick("What is the monthly R3M NPS share of TRO in 1L TNBC?")}>
              What is the monthly R3M NPS share of TRO in 1L TNBC?
            </span>
            <span className="example-chip" onClick={() => onChipClick("How is the R6M TPS share trend for TRO in 4L+ HR+?")}>
              How is the R6M TPS share trend for TRO in 4L+ HR+?
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-area">
      <div className="messages-container">
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
