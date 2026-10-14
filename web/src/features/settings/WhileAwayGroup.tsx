/**
 * What happens while nobody is looking: the notification that says a session
 * needs you, and A35's resume after a usage limit resets. Both are switches,
 * and both say in their own row why they cannot be flipped
 * (`docs/DESIGN.md` § "The Settings screen").
 */
import { useEffect, useState } from 'react';
import { Switch } from '../../components/Switch';
import { currentPushState, disablePush, enablePush, type PushState } from '../../push/webpush';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { usePreferences } from '../../stores/preferences';
import { SettingsGroup } from './SettingsGroup';
import { SettingsRow } from './SettingsRow';

export function WhileAwayGroup() {
  return (
    <SettingsGroup title={strings.settings.whileAway}>
      <NotifyRow />
      <ResumeRow />
    </SettingsGroup>
  );
}

function NotifyRow() {
  const config = useAuth((s) => s.config);
  const [push, setPush] = useState<PushState>('unsupported');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void currentPushState().then(setPush);
  }, []);

  // Three ways this browser can do nothing: the gateway serves no push, this
  // browser has none, or the person refused it. The gateway comes first — it is
  // the one a reader can do something about. Each replaces the row's sentence.
  const reason =
    config?.push.web_enabled === false
      ? strings.settings.pushServerDisabled
      : push === 'unsupported'
        ? strings.settings.pushUnsupported
        : push === 'denied'
          ? strings.settings.pushBlocked
          : null;

  return (
    <SettingsRow
      title={strings.settings.notify}
      sentence={reason ?? strings.settings.notifyNote}
      target
      control={
        <Switch
          label={strings.settings.notify}
          checked={push === 'subscribed'}
          disabled={reason !== null || busy}
          onChange={async () => {
            setBusy(true);
            try {
              setPush(push === 'subscribed' ? await disablePush() : await enablePush());
            } catch {
              setPush(await currentPushState());
            } finally {
              setBusy(false);
            }
          }}
        />
      }
    />
  );
}

function ResumeRow() {
  // A35: the account's own switch, which the gateway holds. `undefined` is a
  // gateway that predates it, not a switch that is off.
  const preferences = usePreferences((s) => s.preferences);
  const setResumeAfterLimit = usePreferences((s) => s.setResumeAfterLimit);
  const [error, setError] = useState<string | null>(null);

  const sentence =
    preferences === undefined
      ? strings.settings.resumeUnavailable
      : (error ?? strings.settings.resumeAfterLimitNote);

  return (
    <SettingsRow
      title={strings.settings.resumeAfterLimit}
      sentence={sentence}
      target
      control={
        <Switch
          label={strings.settings.resumeAfterLimit}
          checked={preferences?.resume_after_limit ?? false}
          disabled={preferences === undefined}
          onChange={(next) => {
            setError(null);
            setResumeAfterLimit(next).catch(() => setError(strings.errors.setFailed));
          }}
        />
      }
    />
  );
}
