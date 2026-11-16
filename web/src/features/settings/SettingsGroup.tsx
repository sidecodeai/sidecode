/**
 * Rows on one soft surface, under a caption on the canvas when the section has
 * more than the one thing to say. The caption sits under the section's own
 * title, so it is the third heading level. Nothing else.
 */
import type { ReactNode } from 'react';

export function SettingsGroup({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <section className="settings-group">
      {title ? <h3 className="group-title">{title}</h3> : null}
      <div className="surface">{children}</div>
    </section>
  );
}
