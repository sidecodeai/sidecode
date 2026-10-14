/**
 * The composer's primary slot while the app, and not the person, has the next
 * move: Send's pill with a spinner in it.
 *
 * `docs/DESIGN.md` § "The composer" → **Done becomes a spinner, and the spinner
 * becomes Send**. Deliberately not a button, and not a disabled one either — a
 * control that looks live and does nothing is the one thing the row must never
 * show. It carries Send's fill and Send's geometry so the slot neither moves nor
 * changes height when the words arrive and Send takes it back, and it says in
 * words what is being waited for, because a spinner alone says only that
 * something is happening.
 */
interface Props {
  /** What is being waited for, in the words the status line is using. */
  label: string;
}

export function WorkingPill({ label }: Props) {
  return (
    <span className="working-pill" role="status" aria-label={label}>
      <span className="spinner" aria-hidden />
    </span>
  );
}
