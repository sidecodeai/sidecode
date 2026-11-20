/**
 * A control that shows what is chosen is named for what it is and then the
 * choice (`strings.a11y.choice`): "Device, mac-studio-office, online". This
 * finds one by what it is, whatever it is set to, so a test reads the choice
 * only where the choice is what it checks.
 */
import { strings } from '../src/strings';

export const labelled =
  (label: string) =>
  (name: string): boolean =>
    name === label || name.startsWith(strings.a11y.choice(label, ''));
