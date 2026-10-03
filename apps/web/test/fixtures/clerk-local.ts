// The standalone layout fixture uses FormaAuthProvider in local mode.
// Clerk's Next.js server exports cannot be bundled into a browser-only fixture.
export function UserButton() { return null; }

function unexpectedClerkHook(): never {
  throw new Error("The layout fixture must use local authentication.");
}

export const useAuth = unexpectedClerkHook;
export const useClerk = unexpectedClerkHook;
export const useUser = unexpectedClerkHook;
