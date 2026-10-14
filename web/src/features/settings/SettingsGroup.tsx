/** A caption on the canvas and its rows on one soft surface. Nothing else. */
import type { ReactNode } from 'react';

export function SettingsGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="settings-section">
      <h2 className="group-title">{title}</h2>
      <div className="surface">{children}</div>
    </section>
  );
}
