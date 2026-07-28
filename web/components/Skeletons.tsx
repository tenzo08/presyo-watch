/**
 * Loading and failure states.
 *
 * These are not decoration. The API is a Render free instance that sleeps after 15 minutes
 * and takes 30–60 seconds to wake, so the *first* request of any morning is genuinely slow.
 * A spinner would say "something is happening" for a minute and then, on failure, say
 * nothing at all. A skeleton shows the shape of what is coming, and the error states below
 * distinguish "still waking up" from "actually broken", because those deserve different
 * reactions from a reader.
 */

import type { ApiError } from "@/lib/api";

export function ChartSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="skeleton" style={{ height: 14, width: 220, marginBottom: 14 }} />
      <div className="skeleton" style={{ height: 260, width: "100%" }} />
    </div>
  );
}

export function TableSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="skeleton"
          style={{ height: 15, width: `${94 - index * 6}%`, marginBottom: 10 }}
        />
      ))}
    </div>
  );
}

export function Loading({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div role="status" aria-live="polite">
      <span className="state" style={{ padding: 0, display: "block", marginBottom: 12 }}>
        {label}
      </span>
      {children}
    </div>
  );
}

export function Waking() {
  return (
    <p className="state" role="status" aria-live="polite">
      <strong>Waking the API up.</strong>
      This project runs on free infrastructure, so the server sleeps when nobody is looking
      at it. The first request of the day takes up to a minute. Later ones are quick.
    </p>
  );
}

export function Failed({ error, onRetry }: { error: ApiError | Error; onRetry: () => void }) {
  const kind = "kind" in error ? error.kind : "http";
  const explanation =
    kind === "timeout"
      ? "The API did not answer within 90 seconds. It sleeps when idle, so it may still be waking up — trying again usually works."
      : kind === "offline"
        ? "The API could not be reached at all. It may be redeploying, or you may be offline."
        : "The API answered, but with an error. That is a fault on our side, not yours.";

  return (
    <div className="state" role="alert">
      <strong>Could not load this.</strong>
      {explanation}
      <br />
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="state">
      <strong>Nothing to show.</strong>
      {children}
    </p>
  );
}
