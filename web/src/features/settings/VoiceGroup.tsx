/**
 * A29, A65 — the Voice section: the AI features that send what the person says
 * or dictates to a third-party AI service, each a switch with the sentence that
 * names the service, and each asking first: dictation, whether a model tidies it
 * up before it is sent, and the voice assistant. One surface with no caption,
 * since the section's title already says Voice. A gateway that offers no
 * transcription and a gateway with no polish model each say so in their own
 * row; the surface never collapses to a note.
 *
 * The polish switch reads on while the account's polish is on and this browser
 * holds its permission: turning it on asks, then writes it on; turning it off
 * withdraws the permission and writes it off.
 */
import { useEffect, useState } from 'react';
import { Menu } from '../../components/Popover';
import { Segmented } from '../../components/Segmented';
import { Switch } from '../../components/Switch';
import { api } from '../../lib/api';
import type { PolishModel, PolishStrength } from '../../protocol/types';
import { strings } from '../../strings';
import { useConsent, withdrawConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { useConnection } from '../../stores/connection';
import { useSettings } from '../../stores/settings';
import { PolishConsent } from '../consent/PolishConsent';
import { useProviderNames } from '../consent/providerNames';
import { LiveSettingRow } from '../live/LiveSettingRow';
import { DictationRow } from './DictationRow';
import { SettingsGroup } from './SettingsGroup';
import { SettingsRow } from './SettingsRow';

export function VoiceGroup() {
  const polish = useConnection((s) => s.polish);
  const polishEnabled = useSettings((s) => s.polishEnabled);
  const setPolishEnabled = useSettings((s) => s.setPolishEnabled);
  const username = useAuth((s) => s.username);
  const names = useProviderNames();
  const polishHeld = useConsent(username, 'polish', names.polish);
  const [askPolish, setAskPolish] = useState(false);
  // What the switch shows and what the rows under it follow: the account's
  // choice, and this browser's permission to send what is polished.
  const polishOn = polish.enabled && polishEnabled && polishHeld;

  return (
    <>
      <SettingsGroup>
        {/* A44, A65: the web only transcribes on the gateway, whose provider
            detects the language, so its one choice is the permission to send
            the audio there: no menu, and no language anywhere. */}
        <DictationRow />

        {/* The switch is always drawn, so the feature exists even where this
            gateway cannot offer it; the model and the strength appear once it is
            on, because they mean nothing while it is off. */}
        <SettingsRow
          title={strings.settings.polish}
          sentence={
            polish.enabled ? strings.settings.polishNote(names.polish) : strings.settings.polishServerDisabled
          }
          target
          control={
            <Switch
              label={strings.settings.polish}
              checked={polishOn}
              disabled={!polish.enabled}
              onChange={(next) => {
                if (!next) {
                  if (username) withdrawConsent(username, 'polish');
                  setPolishEnabled(false);
                } else setAskPolish(true);
              }}
            />
          }
        />

        {polishOn ? <PolishRows /> : null}

        <LiveSettingRow />
      </SettingsGroup>

      <PolishConsent
        open={askPolish}
        onClose={() => setAskPolish(false)}
        onAgreed={() => setPolishEnabled(true)}
      />
    </>
  );
}

function PolishRows() {
  const polishModel = useSettings((s) => s.polishModel);
  const setPolishModel = useSettings((s) => s.setPolishModel);
  const polishStrength = useSettings((s) => s.polishStrength);
  const setPolishStrength = useSettings((s) => s.setPolishStrength);
  const [models, setModels] = useState<PolishModel[]>([]);
  const [failed, setFailed] = useState(false);

  // A29: the list belongs to the gateway's provider, so it is asked for when
  // these rows are drawn. A gateway that lists nothing leaves the menu where it
  // is, with what was chosen before; a list with nothing chosen yet settles on
  // its first model, so the switch is all a first-time reader has to touch.
  useEffect(() => {
    let live = true;
    api
      .polishModels()
      .then((result) => {
        if (!live) return;
        setModels(result.models);
        setFailed(false);
        const first = result.models[0];
        if (first && useSettings.getState().polishModel.length === 0) setPolishModel(first.id);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [setPolishModel]);

  // The model chosen, by its name in the list, or by its id while there is no list.
  const chosen = models.find((model) => model.id === polishModel)?.label || polishModel;

  return (
    <>
      <SettingsRow
        title={strings.settings.polishModel}
        sentence={failed ? strings.settings.polishModelsFailed : strings.settings.polishModelNote}
        target
        control={
          <Menu
            align="end"
            ariaLabel={strings.settings.polishModel}
            value={polishModel}
            onSelect={setPolishModel}
            options={models.map((model) => ({ id: model.id, label: model.label }))}
            label={chosen || strings.settings.polishChooseModel}
            valueText={chosen || undefined}
          />
        }
      />
      <SettingsRow
        title={strings.settings.polishStrength}
        sentence={strings.settings.polishStrengthNote}
        control={
          <Segmented<PolishStrength>
            ariaLabel={strings.settings.polishStrength}
            value={polishStrength}
            onChange={setPolishStrength}
            options={[
              { value: 'moderate', label: strings.settings.polishModerate },
              { value: 'strong', label: strings.settings.polishStrong },
            ]}
          />
        }
      />
    </>
  );
}
