/**
 * A65 — Settings › Voice › Voice assistant: a switch that reads the permission this browser holds
 * for the account to send a call to the AI speech service the gateway names, with the sentence that
 * names it. On asks first; off withdraws the permission and ends a call that is open. Drawn where the
 * gateway offers the assistant to the account, and wherever the permission is held.
 */
import { useState } from 'react';
import { Switch } from '../../components/Switch';
import { useConsent, withdrawConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { SettingsRow } from '../settings/SettingsRow';
import { hangUp } from './liveCall';
import { LiveConsent } from './LiveConsent';
import { liveStrings as t } from './strings';
import { useLiveStatus } from './useLiveStatus';

export function LiveSettingRow() {
  const username = useAuth((s) => s.username);
  const { status } = useLiveStatus(username ?? '', false);
  const provider = status?.provider ?? null;
  const held = useConsent(username, 'assistant', provider);
  const [asking, setAsking] = useState(false);
  if (!status?.enabled && !held) return null;

  return (
    <>
      <SettingsRow
        title={t.title}
        sentence={t.settingNote(provider)}
        target
        control={
          <Switch
            label={t.title}
            checked={held}
            onChange={(next) => {
              if (next) setAsking(true);
              else {
                if (username) withdrawConsent(username, 'assistant');
                hangUp();
              }
            }}
          />
        }
      />
      <LiveConsent provider={asking ? provider : undefined} onClose={() => setAsking(false)} />
    </>
  );
}
