/**
 * Chat input with typeahead suggestions
 */

import { useState, useRef, useEffect, useCallback } from "react";
import { Suggestions } from "./Suggestions";
import { fetchSuggestions } from "../api";
import type { Suggestion } from "../types";
import { SpinnerWhite } from "./Spinner";

interface Props {
  onSubmit: (question: string, suggestion?: Suggestion) => void;
  isProcessing: boolean;
}

export function ChatInput({ onSubmit, isProcessing }: Props) {
  const [value, setValue] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const handleInputChange = useCallback((text: string) => {
    setValue(text);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (text.trim().length < 2) {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const results = await fetchSuggestions(text);
        setSuggestions(results);
        setShowSuggestions(results.length > 0);
      } catch {
        setSuggestions([]);
        setShowSuggestions(false);
      }
    }, 150);
  }, []);

  const handleSubmit = useCallback(() => {
    if (!value.trim() || isProcessing) return;
    onSubmit(value.trim());
    setValue("");
    setSuggestions([]);
    setShowSuggestions(false);
  }, [value, isProcessing, onSubmit]);

  const handleSuggestionClick = useCallback((suggestion: Suggestion) => {
    onSubmit(suggestion.label, suggestion);
    setValue("");
    setSuggestions([]);
    setShowSuggestions(false);
  }, [onSubmit]);

  const handleSuggestionFill = useCallback((suggestion: Suggestion) => {
    setValue(suggestion.label);
    setShowSuggestions(false);
    inputRef.current?.focus();
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
    if (e.key === "Escape") {
      setShowSuggestions(false);
    }
  }, [handleSubmit]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (!(e.target as Element).closest(".input-area")) {
        setShowSuggestions(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="input-area">
      {showSuggestions && suggestions.length > 0 && (
        <Suggestions
          items={suggestions}
          onSelect={handleSuggestionClick}
          onFill={handleSuggestionFill}
        />
      )}
      <div className="input-row">
        <input
          ref={inputRef}
          type="text"
          className="chat-input"
          value={value}
          onChange={(e) => handleInputChange(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isProcessing ? "Processing..." : "Ask about ONC Analytics eg. NPS/TPS share, Sales..."}
          disabled={isProcessing}
          autoComplete="off"
        />
        <button
          className="send-btn"
          onClick={handleSubmit}
          disabled={isProcessing || !value.trim()}
          title="Send (Enter)"
        >
          {isProcessing ? (
            <SpinnerWhite size={16} />
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
            </svg>
          )}
        </button>
      </div>
    </div>
  );
}
