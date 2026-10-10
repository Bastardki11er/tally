// One Idempotency-Key per user intent, not per HTTP attempt.
//
// A double click, an Enter pressed twice, or a retry after a timeout is the same
// intent, so it must reuse the key: then the server moves the money at most once.
// The key is kept until the server gives a definite answer, and dropped after it,
// so the next action gets a fresh one.

export class ApiError extends Error {
  constructor(status, problem) {
    super(problem?.detail || `HTTP ${status}`);
    this.status = status; // 0: the request never got an answer
    this.code = problem?.code;
  }
}

/** True if the outcome is unknown or the server asked for a retry: keep the key. */
export const isRetryable = (error) =>
  error.status === 0 || error.status >= 500 || error.code === "IDEMPOTENCY_KEY_IN_USE";

/**
 * Returns ``run(scope, method, path, body)``. ``scope`` names one user action
 * ("transfer", "reverse:<id>"). While a scope has a request in flight, more calls
 * for it are ignored and resolve to undefined.
 */
export function createActions(request, newKey = () => crypto.randomUUID()) {
  const keys = new Map(); // scope -> key of an action still waiting for a definite answer
  const running = new Set();

  return async function run(scope, method, path, body) {
    if (running.has(scope)) return undefined;
    running.add(scope);
    if (!keys.has(scope)) keys.set(scope, newKey());
    try {
      const result = await request(method, path, body, keys.get(scope));
      keys.delete(scope);
      return result;
    } catch (error) {
      if (!isRetryable(error)) keys.delete(scope);
      throw error;
    } finally {
      running.delete(scope);
    }
  };
}
