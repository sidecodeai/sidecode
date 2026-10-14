import { strings } from '../strings';

/**
 * A47: the red dot of a session that stopped working and waits for the person,
 * until someone on the account opens it. The dot is drawn for the eye alone;
 * what a screen reader hears, "not yet opened", is the hidden sentence beside
 * it, which the row's control names with `aria-describedby`, so it is said
 * after the row's name rather than in the middle of it.
 */
export function UnseenDot({ id }: { id: string }) {
  return (
    <>
      <span className="unseen-dot" aria-hidden="true" />
      <span id={id} hidden>
        {strings.sessions.unseen}
      </span>
    </>
  );
}
