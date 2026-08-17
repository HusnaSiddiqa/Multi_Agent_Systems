import { Component, type ReactNode } from "react";

interface Props { children: ReactNode; fallback?: ReactNode; }
interface State { hasError: boolean; }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(err: Error) {
    console.error("[ErrorBoundary]", err);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback ?? (
        <div className="viz-empty"><p>Unable to render visualization.</p></div>
      );
    }
    return this.props.children;
  }
}
