/**
 * The sections of Settings, in the order the nav and the narrow list draw them,
 * each at `/settings/<id>`.
 */
import { CircleUserRound, Mic, SlidersHorizontal, type LucideIcon } from 'lucide-react';
import { strings } from '../../strings';

export type SectionId = 'general' | 'voice' | 'account';

export interface Section {
  id: SectionId;
  icon: LucideIcon;
}

export const SECTIONS: Section[] = [
  { id: 'general', icon: SlidersHorizontal },
  { id: 'voice', icon: Mic },
  { id: 'account', icon: CircleUserRound },
];

export const sectionPath = (id: SectionId): string => `/settings/${id}`;

/** The section's name: the nav's label and the narrow screen's title. */
export function sectionLabel(id: SectionId): string {
  const labels: Record<SectionId, string> = {
    general: strings.settings.general,
    voice: strings.settings.voice,
    account: strings.settings.account,
  };
  return labels[id];
}

/** The sentence under the section's row in the narrow list. */
export function sectionNote(id: SectionId): string {
  const notes: Record<SectionId, string> = {
    general: strings.settings.generalNote,
    voice: strings.settings.voiceNote,
    account: strings.settings.accountNote,
  };
  return notes[id];
}
