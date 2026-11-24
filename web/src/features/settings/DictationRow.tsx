/**
 * A65 — Settings › Voice › Dictation. The web transcribes only through the gateway, so its one
 * control is the permission to send the audio there: a switch that reads the permission this browser
 * holds for the account, where on asks first and off withdraws. It is a choice wherever the gateway
 * transcribes and wherever the permission is held; on a gateway that transcribes nothing it is drawn
 * disabled, and says so.
 */
import { useState } from 'react';
import { Switch } from '../../components/Switch';
import { strings } from '../../strings';
import { useConsent, withdrawConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';
import { DictationConsent } from '../consent/DictationConsent';
import { useProviderNames } from '../consent/providerNames';
import { SettingsRow } from './SettingsRow';

export function DictationRow() {
  const transcribes = useConnection((s) => s.stt.enabled);
  const username = useAuth((s) => s.username);
  const name = useProviderNames().dictation;
  const held = useConsent(username, 'dictation', name);
  const [asking, setAsking] = useState(false);
  const s = strings.settings;
  const usable = transcribes || held;

  return (
    <>
      <SettingsRow
        title={s.dictation}
        sentence={usable ? s.dictationNote(name) : s.voiceServerDisabled}
        target
        control={
          <Switch
            label={s.dictation}
            checked={held}
            disabled={!usable}
            onChange={(next) => {
              if (next) setAsking(true);
              else if (username) withdrawConsent(username, 'dictation');
            }}
          />
        }
      />
      <DictationConsent open={asking} onClose={() => setAsking(false)} />
    </>
  );
}
