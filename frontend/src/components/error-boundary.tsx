"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keeps one broken panel from taking down the whole dashboard. */
export class ErrorBoundary extends Component<{ label: string; children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.label}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card" role="alert">
        <div className="kick">{this.props.label}</div>
        <p className="mt-1 text-sm">This panel failed to render: {this.state.error.message}</p>
        <button type="button" className="btn btn-ghost mt-3" onClick={() => this.setState({ error: null })}>Retry</button>
      </div>
    );
  }
}
