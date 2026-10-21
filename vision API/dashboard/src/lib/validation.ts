// Client-side checks mirroring the API's rules, for instant feedback. The API re-validates.

export const PASSWORD_MIN = 10;
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/;

export type CredentialErrors = { email?: string; password?: string };

export function validateCredentials(mode: "login" | "signup", email: string, password: string): CredentialErrors {
  const errors: CredentialErrors = {};
  if (!EMAIL_PATTERN.test(email.trim())) errors.email = "Enter a valid email address.";
  if (!password) errors.password = "Enter your password.";
  else if (mode === "signup" && password.length < PASSWORD_MIN) errors.password = `Use at least ${PASSWORD_MIN} characters.`;
  return errors;
}

export type PasswordChangeErrors = { current?: string; next?: string; confirm?: string };

export function validatePasswordChange(current: string, next: string, confirm: string): PasswordChangeErrors {
  const errors: PasswordChangeErrors = {};
  if (!current) errors.current = "Enter your current password.";
  if (next.length < PASSWORD_MIN) errors.next = `Use at least ${PASSWORD_MIN} characters.`;
  else if (next === current) errors.next = "Choose a password different from the current one.";
  if (confirm !== next) errors.confirm = "The passwords do not match.";
  return errors;
}
