/**
 * A65: the composer records nothing and sends nothing to be polished until the account signed in
 * holds the permission for the provider the gateway names. The suites about what dictation and
 * polish do once they are allowed start from an account that agreed to both, for a gateway that
 * names no provider, which is what they talk to.
 */
import { forgetConsents, grantConsent } from '../src/stores/aiConsent';
import { useAuth } from '../src/stores/auth';

export const VOICE_ACCOUNT = 'voice-tester';

export function agreedToVoice(): void {
  forgetConsents(VOICE_ACCOUNT);
  useAuth.setState({ username: VOICE_ACCOUNT });
  grantConsent(VOICE_ACCOUNT, 'dictation', null);
  grantConsent(VOICE_ACCOUNT, 'polish', null);
}
