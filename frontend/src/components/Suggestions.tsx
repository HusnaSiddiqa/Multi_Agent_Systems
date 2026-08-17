/**
 * Typeahead suggestion dropdown
 */

import type { Suggestion } from "../types";

interface Props {
  items: Suggestion[];
  onSelect: (item: Suggestion) => void;
  onFill: (item: Suggestion) => void;
}

export function Suggestions({ items, onSelect, onFill }: Props) {
  return (
    <div className="suggestions-dropdown">
      {items.map((item, i) => (
        <div key={i} className="suggestion-item">
          <span
            className="suggestion-text"
            onClick={() => onFill(item)}
            title="Fill input"
          >
            {item.label}
          </span>
          <button
            className="suggestion-exec"
            onClick={() => onSelect(item)}
            title="Execute directly"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
