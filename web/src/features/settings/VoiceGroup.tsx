/**
 * A29 — dictation: who transcribes it, and whether a model tidies it up before
 * it is sent. A gateway that offers no transcription and a gateway with no
 * polish model each say so in their own row; the group itself never collapses
 * to a note.
 */
import { useEffect, useState } from 'react';
import { Menu } from '../../components/Popover';
import { Segmented } from '../../components/Segmented';
import { Switch } from '../../components/Switch';
import { api } from '../../lib/api';
import type { PolishModel, PolishStrength } from '../../protocol/types';
import { strings } from '../../strings';
import { useConnection } from '../../stores/connection';
import { useSettings } from '../../stores/settings';
import { SettingsGroup } from './SettingsGroup';
import { SettingsRow } from './SettingsRow';

export function VoiceGroup() {
  const stt = useConnection((s) => s.stt);
  const polish = useConnection((s) => s.polish);
  const polishEnabled = useSettings((s) => s.polishEnabled);
  const setPolishEnabled = useSettings((s) => s.setPolishEnabled);

  return (
    <SettingsGroup title={strings.settings.voice}>
      {/* A44: the web only transcribes on the gateway, whose provider detects
          the language, so the row names the gateway and has nothing to choose:
          no menu here, and no language anywhere. */}
      <SettingsRow
        title={strings.settings.transcribe}
        sentence={
          stt.enabled ? strings.settings.transcribeNote : strings.settings.voiceServerDisabled
        }
        control={<span className="pill quiet">{strings.settings.transcribeGateway}</span>}
      />

      {/* The switch is always drawn, so the feature exists even where this
          gateway cannot offer it; the model and the strength appear once it is
          on, because they mean nothing while it is off. */}
      <SettingsRow
        title={strings.settings.polish}
        sentence={polish.enabled ? strings.settings.polishNote : strings.settings.polishServerDisabled}
        target
        control={
          <Switch
            label={strings.settings.polish}
            checked={polish.enabled && polishEnabled}
            disabled={!polish.enabled}
            onChange={setPolishEnabled}
          />
        }
      />

      {polish.enabled && polishEnabled ? <PolishRows /> : null}
    </SettingsGroup>
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
            label={
              models.find((model) => model.id === polishModel)?.label ||
              polishModel ||
              strings.settings.polishChooseModel
            }
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
