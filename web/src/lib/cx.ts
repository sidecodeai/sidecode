export type ClassValue = string | false | null | undefined;

/** Minimal class-name joiner; avoids a dependency for a five-line helper. */
export function cx(...values: ClassValue[]): string {
  let out = '';
  for (const v of values) {
    if (!v) continue;
    out = out ? `${out} ${v}` : v;
  }
  return out;
}
