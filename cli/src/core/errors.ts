/** Typed errors so the CLI can pick exit codes and skills can react without parsing text. */

export class CompassError extends Error {
  readonly exitCode: number;
  readonly code: string;
  readonly details?: unknown;
  constructor(code: string, message: string, exitCode = 1, details?: unknown) {
    super(message);
    this.name = "CompassError";
    this.code = code;
    this.exitCode = exitCode;
    this.details = details;
  }
}

export class NotConfiguredError extends CompassError {
  constructor(message = "No Compass profile configured. Run `compass login` or `compass install` first.") {
    super("not_configured", message, 5);
  }
}

export class AuthError extends CompassError {
  constructor(message = "Compass rejected the API token. Run `compass login` again.") {
    super("auth", message, 6);
  }
}

export class ScopeError extends CompassError {
  constructor(message: string, details?: unknown) {
    super("scope", message, 6, details);
  }
}

export class LimitReachedError extends CompassError {
  constructor(message: string, details?: unknown) {
    super("limit_reached", message, 3, details);
  }
}

export class InfraScanPendingError extends CompassError {
  constructor(message: string) {
    super("infra_scan_pending", message, 4);
  }
}

export class CaptainUnavailableError extends CompassError {
  readonly captain?: string;
  constructor(message: string, details?: any) {
    const captain = typeof details?.captain === "string" ? details.captain : undefined;
    const code = typeof details?.code === "string" ? details.code : "captain_disabled";
    const hint = code === "captain_unlicensed" ? "" : captain ? ` Turn it on with \`compass captains enable ${captain}\`.` : "";
    super(code, message + hint, 7, details);
    this.captain = captain;
  }
}

export class PlanOnlyError extends CompassError {
  constructor(message = "Plan shown; nothing was executed. Re-run with --yes to apply.") {
    super("plan_only", message, 2);
  }
}

export class ApiError extends CompassError {
  readonly status: number;
  constructor(status: number, message: string, details?: unknown) {
    super("api", message, 1, details);
    this.status = status;
  }
}
